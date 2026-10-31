package bench

import (
	"github.com/open-telemetry/opentelemetry-collector-contrib/processor/tailsamplingprocessor"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

var _ = tailsamplingprocessor.NewFactory // the sampler replays the real processor

// Stored is one span found in ClickHouse after a run.
type Stored struct {
	TraceID [16]byte
	SpanID  [8]byte
	Gateway string // the bench-only resource attribute traceblame.bench.gateway
	Tenant  string
	StartNS int64
}

// Outcome of one trace.
type Outcome string

const (
	// OutcomeStored means every span is in ClickHouse.
	OutcomeStored Outcome = "stored"
	// OutcomeDropped means no span is stored, and the gateway policies drop the whole trace.
	OutcomeDropped Outcome = "dropped"
	// OutcomeSplit means more than one gateway processed the trace; spans not stored belong to
	// parts the policies drop on their own (independent sampling of the halves).
	OutcomeSplit Outcome = "split"
	// OutcomeLost means some span is missing that no drop decision accounts for.
	OutcomeLost Outcome = "lost"
)

// Result accounts for one trace.
type Result struct {
	ID           [16]byte
	Service      string
	Error        bool
	Spans        int
	StoredSpans  int
	DroppedSpans int // missing, accounted for by a drop decision
	LostSpans    int // missing, not accounted for
	Gateways     int
	Outcome      Outcome
}

// Account classifies every trace: spans sent = stored + dropped by decision + lost.
func Account(g *Generator, traces []Trace, rows []Stored, s *TailSampler) ([]Result, error) {
	byTrace := map[[16]byte]map[[8]byte]Stored{}
	for _, r := range rows {
		if byTrace[r.TraceID] == nil {
			byTrace[r.TraceID] = map[[8]byte]Stored{}
		}
		byTrace[r.TraceID][r.SpanID] = r
	}
	// One replay for every trace with missing spans: of the whole trace when nothing was
	// stored, of the missing part otherwise. A part keeps the trace id.
	const start = int64(1_800_000_000_000_000_000)
	var parts []ptrace.Traces
	missing := map[[16]byte][]SpanPlan{}
	for _, t := range traces {
		got := byTrace[t.ID]
		var m []SpanPlan
		for _, sp := range t.Spans {
			if _, ok := got[sp.SpanID]; !ok {
				m = append(m, sp)
			}
		}
		if len(m) == 0 {
			continue
		}
		missing[t.ID] = m
		parts = append(parts, part(g, t, m, start))
	}
	kept, err := s.Decide(parts)
	if err != nil {
		return nil, err
	}

	out := make([]Result, 0, len(traces))
	for _, t := range traces {
		got := byTrace[t.ID]
		gateways := map[string]bool{}
		for _, r := range got {
			gateways[r.Gateway] = true
		}
		r := Result{
			ID: t.ID, Service: t.Service, Error: t.Error, Spans: len(t.Spans),
			StoredSpans: len(got), Gateways: len(gateways),
		}
		m := missing[t.ID]
		switch {
		case len(m) == 0 && len(gateways) > 1:
			r.Outcome = OutcomeSplit
		case len(m) == 0:
			r.Outcome = OutcomeStored
		case kept[t.ID]:
			r.Outcome, r.LostSpans = OutcomeLost, len(m)
		case len(got) == 0:
			r.Outcome, r.DroppedSpans = OutcomeDropped, len(m)
		default:
			r.Outcome, r.DroppedSpans = OutcomeSplit, len(m)
		}
		out = append(out, r)
	}
	return out, nil
}

func part(g *Generator, t Trace, spans []SpanPlan, start int64) ptrace.Traces {
	want := map[[8]byte]bool{}
	for _, s := range spans {
		want[s.SpanID] = true
	}
	td := ptrace.NewTraces()
	for _, b := range g.Build(t, start) {
		if want[[8]byte(b.Traces.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).SpanID())] {
			b.Traces.ResourceSpans().MoveAndAppendTo(td.ResourceSpans())
		}
	}
	return td
}

// Window is a scaling transition: from the scale command, minus the longest trace, to the
// new ring being settled, plus decision_wait.
type Window struct {
	Name     string
	From, To int64
}

// Phase names the window a trace (start, duration) overlaps, or "steady".
func Phase(windows []Window, start, duration int64) string {
	for _, w := range windows {
		if start <= w.To && start+duration >= w.From {
			return w.Name
		}
	}
	return "steady"
}
