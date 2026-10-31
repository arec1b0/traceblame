package main

import (
	"flag"
	"fmt"
	"io"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

// thresholds prints the latency env file of the gateway: per service, the p95 of the trace
// duration of the generated traffic, rounded up to the millisecond; the default for
// unknown services is the largest of them. It also prints, for the record, the p95 of the
// single fixture spans per SDK.
func thresholds(args []string, out io.Writer) error {
	fs := flag.NewFlagSet("thresholds", flag.ContinueOnError)
	var lf loadFlags
	lf.register(fs)
	n := fs.Uint("traces", 100000, "traces to sample per load generator")
	pods := fs.Uint("pods", 2, "load generators")
	date := fs.String("date", time.Now().UTC().Format("2006-01-02"), "date written into the file")
	if err := fs.Parse(args); err != nil {
		return err
	}
	all := map[string][]int64{}
	for pod := uint(0); pod < *pods; pod++ {
		lf.pod = pod
		g, err := lf.generator()
		if err != nil {
			return err
		}
		for svc, v := range g.P95(uint32(min(*n, math.MaxUint32))) {
			all[svc] = append(all[svc], v)
		}
	}
	svcs := make([]string, 0, len(all))
	for s := range all {
		svcs = append(svcs, s)
	}
	sort.Strings(svcs)

	fixtureP95, err := fixtureSpanP95(lf.fixtures)
	if err != nil {
		return err
	}
	w := func(format string, a ...any) { _, _ = fmt.Fprintf(out, format, a...) }
	w("# Tail sampling \"latency > p95\" thresholds of the gateway (design §1.1), in ms.\n")
	w("# Source: `loadgen thresholds` on %s: trace durations (root span) of the generated\n", *date)
	w("# bench traffic, %d traces x %d load generators, seed %d, %d spans per trace; each trace\n", *n, *pods, lf.seed, lf.params.SpansPerTrace)
	w("# replays real content-off golden fixture spans of its service's SDK, with their durations\n")
	w("# scaled by a deterministic +-20%% jitter. Per service: p95 rounded up to the millisecond;\n")
	w("# the default for unknown services is the largest. For the record, p95 of the single\n")
	w("# content-off fixture spans per SDK (16 real captures): ")
	sdks := make([]string, 0, len(fixtureP95))
	for s := range fixtureP95 {
		sdks = append(sdks, s)
	}
	sort.Strings(sdks)
	for i, s := range sdks {
		if i > 0 {
			w(", ")
		}
		w("%s %d ms", s, fixtureP95[s])
	}
	w(".\n# #21 replaces these values from representative traffic.\n")
	var maxMS int64
	for _, svc := range svcs {
		var v int64
		for _, x := range all[svc] {
			v = max(v, x)
		}
		ms := v / int64(time.Millisecond)
		maxMS = max(maxMS, ms)
		w("LATENCY_P95_MS_%s=%d\n", strings.ToUpper(strings.ReplaceAll(svc, "-", "_")), ms)
	}
	w("LATENCY_P95_MS_DEFAULT=%d\n", maxMS)
	return nil
}

func fixtureSpanP95(root string) (map[string]int64, error) {
	all, err := fixtures.Discover(root)
	if err != nil {
		return nil, err
	}
	by := map[string][]int64{}
	for _, fx := range all {
		if strings.HasSuffix(fx.ID, "_content") {
			continue
		}
		td, err := fx.Read()
		if err != nil {
			return nil, err
		}
		s := td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0)
		by[fx.SDK()] = append(by[fx.SDK()], s.EndTimestamp().AsTime().Sub(s.StartTimestamp().AsTime()).Nanoseconds())
	}
	out := map[string]int64{}
	for sdk, d := range by {
		sort.Slice(d, func(i, j int) bool { return d[i] < d[j] })
		v := d[(len(d)*95+99)/100-1]
		out[sdk] = (v + int64(time.Millisecond) - 1) / int64(time.Millisecond)
	}
	return out, nil
}
