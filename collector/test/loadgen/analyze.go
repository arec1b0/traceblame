package main

import (
	"bufio"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/arec1b0/traceblame/collector/internal/bench"
)

// Event is one scaling step recorded by bench/gateway-scaling/run.sh.
type Event struct {
	Name      string `json:"name"`
	CommandNS int64  `json:"command_ns"`
	SettledNS int64  `json:"settled_ns"`
}

// PhaseStats aggregates the traces of one phase.
type PhaseStats struct {
	Traces          int     `json:"traces"`
	Spans           int     `json:"spans"`
	Stored          int     `json:"stored"`
	Dropped         int     `json:"dropped"`
	Split           int     `json:"split"`
	Lost            int     `json:"lost"`
	LostSpans       int     `json:"lost_spans"`
	DroppedSpans    int     `json:"dropped_spans"`
	ErrorSplit      int     `json:"error_split"`
	ErrorSplitWhole int     `json:"error_split_complete"`
	SplitRate       float64 `json:"split_rate"`
	SplitFromMS     float64 `json:"split_window_from_ms,omitempty"`
	SplitToMS       float64 `json:"split_window_to_ms,omitempty"`
	// Ring update: scale command until the Deployment reports the new replica count. The
	// split window is expected to stay within the ring update plus decision_wait (#10).
	RingUpdateMS    float64 `json:"ring_update_ms,omitempty"`
	ExpectedSplitMS float64 `json:"expected_split_window_max_ms,omitempty"`
	SplitWithin     *bool   `json:"split_window_within_expected,omitempty"`
}

// Report is the JSON written next to the Markdown report.
type Report struct {
	Environment   map[string]string      `json:"environment"`
	Load          []Summary              `json:"load"`
	Phases        map[string]*PhaseStats `json:"phases"`
	Latency       map[string]LatencyStat `json:"latency_policy"`
	TenantErrors  int                    `json:"tenant_mismatches"`
	UnsentSpans   int                    `json:"unsent_spans"`
	SpansSent     int                    `json:"spans_sent"`
	SpansStored   int                    `json:"spans_stored"`
	SpansDropped  int                    `json:"spans_dropped_by_decision"`
	SpansLost     int                    `json:"spans_lost"`
	Hard          map[string]bool        `json:"hard_criteria"`
	DecisionWait  string                 `json:"decision_wait"`
	ScaleDownLoss int                    `json:"scale_down_lost_spans"`
}

// LatencyStat: share of a service's traces slower than its threshold.
type LatencyStat struct {
	ThresholdMS int     `json:"threshold_ms"`
	Traces      int     `json:"traces"`
	Slow        int     `json:"slow"`
	Share       float64 `json:"share"`
	SlowStored  int     `json:"slow_stored"`
}

func readEnvFile(path string) (map[string]string, error) {
	raw, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if ok {
			out[k] = v
		}
	}
	return out, nil
}

func readStored(path string) ([]bench.Stored, error) {
	f, err := os.Open(filepath.Clean(path))
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }()
	var out []bench.Stored
	s := bufio.NewScanner(f)
	s.Buffer(make([]byte, 1<<20), 1<<20)
	for s.Scan() {
		cols := strings.Split(s.Text(), "\t")
		if len(cols) != 5 {
			return nil, fmt.Errorf("stored: want 5 columns, got %q", s.Text())
		}
		var r bench.Stored
		tid, err := hex.DecodeString(cols[0])
		if err != nil || len(tid) != 16 {
			return nil, fmt.Errorf("stored: trace id %q", cols[0])
		}
		sid, err := hex.DecodeString(cols[1])
		if err != nil || len(sid) != 8 {
			return nil, fmt.Errorf("stored: span id %q", cols[1])
		}
		r.TraceID, r.SpanID = [16]byte(tid), [8]byte(sid)
		r.Gateway, r.Tenant = cols[2], cols[3]
		r.StartNS, _ = strconv.ParseInt(cols[4], 10, 64)
		out = append(out, r)
	}
	return out, s.Err()
}

func readJSONLines[T any](path string) ([]T, error) {
	raw, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		return nil, err
	}
	var out []T
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var v T
		if err := json.Unmarshal([]byte(line), &v); err != nil {
			return nil, fmt.Errorf("%s: %w", path, err)
		}
		out = append(out, v)
	}
	return out, nil
}

func analyze(args []string, out io.Writer) error {
	fs := flag.NewFlagSet("analyze", flag.ContinueOnError)
	fixturesDir := fs.String("fixtures", "testdata/fixtures", "golden fixtures directory")
	gatewayConfig := fs.String("gateway-config", "", "the gateway collector config of the run")
	thresholdsPath := fs.String("thresholds", "", "latency thresholds env file of the run")
	summaries := fs.String("summaries", "", "loadgen summaries, one JSON line per pod")
	storedPath := fs.String("stored", "", "stored spans TSV: trace_id span_id gateway tenant start_ns")
	eventsPath := fs.String("events", "", "scaling events JSON lines")
	envPath := fs.String("env", "", "environment description, JSON object")
	decisionWait := fs.Duration("decision-wait", 30*time.Second, "gateway tail_sampling decision_wait")
	outDir := fs.String("out", ".", "report directory")
	name := fs.String("name", "gateway-scaling", "report file name, without extension")
	if err := fs.Parse(args); err != nil {
		return err
	}
	env, err := readEnvFile(*thresholdsPath)
	if err != nil {
		return err
	}
	sampler, err := bench.NewTailSampler(*gatewayConfig, env)
	if err != nil {
		return err
	}
	loads, err := readJSONLines[Summary](*summaries)
	if err != nil {
		return err
	}
	events, err := readJSONLines[Event](*eventsPath)
	if err != nil {
		return err
	}
	rows, err := readStored(*storedPath)
	if err != nil {
		return err
	}
	environment := map[string]string{}
	if *envPath != "" {
		raw, err := os.ReadFile(*envPath)
		if err != nil {
			return err
		}
		if err := json.Unmarshal(raw, &environment); err != nil {
			return err
		}
	}

	rep := Report{
		Environment: environment, Load: loads, Phases: map[string]*PhaseStats{},
		Latency: map[string]LatencyStat{}, Hard: map[string]bool{}, DecisionWait: decisionWait.String(),
	}
	tenantByPod := map[uint8]string{}
	type placed struct {
		trace bench.Trace
		start int64
	}
	var all []placed
	var results []bench.Result
	var maxDuration int64
	for _, l := range loads {
		tenantByPod[l.Pod] = l.Tenant
		rep.UnsentSpans += l.UnsentSpans
		g, err := bench.NewGenerator(bench.Params{
			RunID: l.RunID, Pod: l.Pod, SpansPerTrace: l.SpansPer, ErrorEvery: l.ErrorEvery, Seed: l.Seed,
		}, *fixturesDir)
		if err != nil {
			return err
		}
		interval := float64(time.Second) / l.Rate
		var traces []bench.Trace
		for seq := uint32(0); seq < l.Traces; seq++ {
			t := g.Trace(seq)
			traces = append(traces, t)
			all = append(all, placed{t, l.T0 + int64(float64(seq)*interval)})
			maxDuration = max(maxDuration, t.Duration)
		}
		r, err := bench.Account(g, traces, rows, sampler)
		if err != nil {
			return err
		}
		results = append(results, r...)
	}

	var windows []bench.Window
	for _, e := range events {
		windows = append(windows, bench.Window{
			Name: e.Name, From: e.CommandNS - maxDuration,
			To: e.SettledNS + decisionWait.Nanoseconds() + 5*int64(time.Second),
		})
	}
	commandOf := map[string]int64{}
	for _, e := range events {
		commandOf[e.Name] = e.CommandNS
	}
	for i, r := range results {
		p := all[i]
		phase := bench.Phase(windows, p.start, p.trace.Duration)
		st := rep.Phases[phase]
		if st == nil {
			st = &PhaseStats{}
			rep.Phases[phase] = st
		}
		st.Traces++
		st.Spans += r.Spans
		st.LostSpans += r.LostSpans
		st.DroppedSpans += r.DroppedSpans
		rep.SpansSent += r.Spans
		rep.SpansStored += r.StoredSpans
		rep.SpansDropped += r.DroppedSpans
		rep.SpansLost += r.LostSpans
		switch r.Outcome {
		case bench.OutcomeStored:
			st.Stored++
		case bench.OutcomeDropped:
			st.Dropped++
		case bench.OutcomeLost:
			st.Lost++
		case bench.OutcomeSplit:
			st.Split++
			if r.Error {
				st.ErrorSplit++
				if r.StoredSpans == r.Spans {
					st.ErrorSplitWhole++
				}
			}
			if cmd, ok := commandOf[phase]; ok {
				rel := float64(p.start-cmd) / 1e6
				if st.Split == 1 || rel < st.SplitFromMS {
					st.SplitFromMS = rel
				}
				st.SplitToMS = max(st.SplitToMS, rel)
			}
		}
		if strings.Contains(phase, "->2") || strings.Contains(phase, "down") {
			rep.ScaleDownLoss += r.LostSpans
		}
	}
	for _, e := range events {
		st := rep.Phases[e.Name]
		if st == nil {
			continue
		}
		st.RingUpdateMS = float64(e.SettledNS-e.CommandNS) / 1e6
		st.ExpectedSplitMS = st.RingUpdateMS + float64(decisionWait.Milliseconds())
		if st.Split > 0 {
			within := st.SplitToMS-st.SplitFromMS <= st.ExpectedSplitMS
			st.SplitWithin = &within
		}
	}
	for _, st := range rep.Phases {
		if st.Traces > 0 {
			st.SplitRate = float64(st.Split) / float64(st.Traces)
		}
	}
	storedByTrace := map[[16]byte]int{}
	for _, row := range rows {
		storedByTrace[row.TraceID]++
		if want := tenantByPod[bench.ParseTraceID(row.TraceID).Pod]; row.Tenant != want {
			rep.TenantErrors++
		}
	}
	for svc := range bench.ServiceSDK {
		name := "LATENCY_P95_MS_" + strings.ToUpper(strings.ReplaceAll(svc, "-", "_"))
		ms, _ := strconv.Atoi(env[name])
		ls := LatencyStat{ThresholdMS: ms}
		for _, p := range all {
			if p.trace.Service != svc {
				continue
			}
			ls.Traces++
			if p.trace.Duration > int64(ms)*int64(time.Millisecond) {
				ls.Slow++
				if storedByTrace[p.trace.ID] == len(p.trace.Spans) {
					ls.SlowStored++
				}
			}
		}
		if ls.Traces > 0 {
			ls.Share = float64(ls.Slow) / float64(ls.Traces)
		}
		rep.Latency[svc] = ls
	}
	steady := rep.Phases["steady"]
	rep.Hard["no_spans_lost_in_transport"] = rep.SpansLost == 0 && rep.UnsentSpans == 0
	rep.Hard["no_split_traces_in_steady_state"] = steady == nil || steady.Split == 0
	rep.Hard["no_spans_lost_on_scale_down"] = rep.ScaleDownLoss == 0
	rep.Hard["tenant_from_credentials"] = rep.TenantErrors == 0

	if err := os.MkdirAll(*outDir, 0o750); err != nil {
		return err
	}
	js, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(*outDir, *name+".json"), append(js, '\n'), 0o600); err != nil {
		return err
	}
	md := markdown(rep, windows)
	if err := os.WriteFile(filepath.Join(*outDir, *name+".md"), []byte(md), 0o600); err != nil {
		return err
	}
	_, err = io.WriteString(out, md)
	return err
}

func pass(ok bool) string {
	if ok {
		return "PASS"
	}
	return "FAIL"
}

func markdown(r Report, windows []bench.Window) string {
	var b strings.Builder
	w := func(format string, a ...any) { fmt.Fprintf(&b, format, a...) }
	w("# Gateway scaling 2 → 4 → 2 under steady load (issue #10)\n\n")
	w("Generated by `bench/gateway-scaling/run.sh` (`loadgen analyze`); the raw numbers are in the JSON next to this file.\n\n")
	w("## Environment\n\n| Key | Value |\n|---|---|\n")
	keys := make([]string, 0, len(r.Environment))
	for k := range r.Environment {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		w("| %s | %s |\n", k, r.Environment[k])
	}
	w("\n## Load\n\n| Pod | Tenant | Rate (traces/s) | Traces | Spans/trace | Error every | Spans sent | Unsent | Export errors (retried) |\n|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
	for _, l := range r.Load {
		w("| %d | %s | %.0f | %d | %d | %d | %d | %d | %d |\n", l.Pod, l.Tenant, l.Rate, l.Traces, l.SpansPer, l.ErrorEvery, l.SentSpans, l.UnsentSpans, l.ExportErrors)
	}
	w("\n## Hard criteria\n\n| Criterion | Result |\n|---|---|\n")
	w("| 0 spans lost in transport (every span stored, or dropped by a tail_sampling decision) | %s (%d lost, %d unsent) |\n", pass(r.Hard["no_spans_lost_in_transport"]), r.SpansLost, r.UnsentSpans)
	steadySplit := 0
	if s := r.Phases["steady"]; s != nil {
		steadySplit = s.Split
	}
	w("| 0 split traces in steady state | %s (%d) |\n", pass(r.Hard["no_split_traces_in_steady_state"]), steadySplit)
	w("| 0 spans lost on scale-down (drain: preStop + grace >= decision_wait + export timeout) | %s (%d lost) |\n", pass(r.Hard["no_spans_lost_on_scale_down"]), r.ScaleDownLoss)
	w("| tenant = authenticated credentials, never the shim | %s (%d mismatches) |\n", pass(r.Hard["tenant_from_credentials"]), r.TenantErrors)
	w("\nSpans: %d sent = %d stored + %d dropped by decision + %d lost.\n", r.SpansSent, r.SpansStored, r.SpansDropped, r.SpansLost)
	w("\n## Phases\n\nA trace belongs to a transition when its lifetime overlaps [scale command − longest trace, ring settled + decision_wait (%s) + 5 s].\n\n", r.DecisionWait)
	w("Split window: start times of the split traces, relative to the scale command. Expected length <= ring update (command until the new replica count is ready) + decision_wait.\n\n")
	w("| Phase | Traces | Stored | Dropped | Split | Lost | Split rate | Split window (ms after command) | Ring update (ms) | Window length / expected max (ms) | ERROR traces split | of which complete |\n|---|---:|---:|---:|---:|---:|---:|---|---:|---|---:|---:|\n")
	names := []string{"steady"}
	for _, win := range windows {
		names = append(names, win.Name)
	}
	for _, n := range names {
		s := r.Phases[n]
		if s == nil {
			continue
		}
		window, ring, length := "-", "-", "-"
		if n != "steady" {
			ring = fmt.Sprintf("%.0f", s.RingUpdateMS)
		}
		if s.Split > 0 && n != "steady" {
			window = fmt.Sprintf("%.0f .. %.0f", s.SplitFromMS, s.SplitToMS)
			verdict := "within"
			if s.SplitWithin != nil && !*s.SplitWithin {
				verdict = "LARGER than expected"
			}
			length = fmt.Sprintf("%.0f / %.0f (%s)", s.SplitToMS-s.SplitFromMS, s.ExpectedSplitMS, verdict)
		}
		w("| %s | %d | %d | %d | %d | %d | %.4f | %s | %s | %s | %d | %d |\n", n, s.Traces, s.Stored, s.Dropped, s.Split, s.Lost, s.SplitRate, window, ring, length, s.ErrorSplit, s.ErrorSplitWhole)
	}
	w("\n## Latency policy (target ≈ 5%% of each service's traces)\n\n| Service | threshold_ms | Traces | Slower | Share | Slower and fully stored |\n|---|---:|---:|---:|---:|---:|\n")
	svcs := make([]string, 0, len(r.Latency))
	for s := range r.Latency {
		svcs = append(svcs, s)
	}
	sort.Strings(svcs)
	for _, s := range svcs {
		l := r.Latency[s]
		w("| %s | %d | %d | %d | %.3f | %d |\n", s, l.ThresholdMS, l.Traces, l.Slow, l.Share, l.SlowStored)
	}
	return b.String()
}
