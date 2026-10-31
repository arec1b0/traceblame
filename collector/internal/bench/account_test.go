package bench

import (
	"path/filepath"
	"runtime"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

func gatewayConfig() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", "..", "..", "deploy", "kind", "collector", "gateway.yaml")
}

var thresholds = map[string]string{
	"LATENCY_P95_MS_BENCH_OPENINFERENCE": "5000",
	"LATENCY_P95_MS_BENCH_OPENLLMETRY":   "5000",
	"LATENCY_P95_MS_BENCH_OTEL_GENAI":    "5000",
	"LATENCY_P95_MS_DEFAULT":             "5000",
}

func sampler(t *testing.T) *TailSampler {
	t.Helper()
	s, err := NewTailSampler(gatewayConfig(), thresholds)
	require.NoError(t, err)
	return s
}

func whole(g *Generator, t Trace) ptrace.Traces {
	td := ptrace.NewTraces()
	for _, b := range g.Build(t, 1_800_000_000_000_000_000) {
		b.Traces.ResourceSpans().MoveAndAppendTo(td.ResourceSpans())
	}
	return td
}

func TestTheSamplerReplaysTheGatewayPolicies(t *testing.T) {
	g := generator(t, params)
	var parts []ptrace.Traces
	var traces []Trace
	for seq := uint32(0); seq < 40; seq++ {
		tr := g.Trace(seq)
		traces = append(traces, tr)
		parts = append(parts, whole(g, tr))
	}
	sampled, err := sampler(t).Decide(parts)
	require.NoError(t, err)
	for _, tr := range traces {
		if tr.Error {
			assert.True(t, sampled[tr.ID], "every error trace is sampled")
		}
	}
	ok := 0
	for _, tr := range traces {
		if !tr.Error && sampled[tr.ID] {
			ok++
		}
	}
	assert.Less(t, ok, 20, "most ok traces are dropped (10%% plus slow ones)")
}

// stored returns the stored rows for the spans of tr selected by keep, all via gateway gw.
func stored(tr Trace, gw string, keep func(SpanPlan) bool) []Stored {
	var out []Stored
	for _, s := range tr.Spans {
		if keep(s) {
			out = append(out, Stored{TraceID: tr.ID, SpanID: s.SpanID, Gateway: gw, Tenant: "tenant-b"})
		}
	}
	return out
}

func TestAccountClassifiesEveryTrace(t *testing.T) {
	g := generator(t, params)
	s := sampler(t)
	var traces []Trace
	var parts []ptrace.Traces
	for seq := uint32(0); seq < 60; seq++ {
		tr := g.Trace(seq)
		traces = append(traces, tr)
		parts = append(parts, whole(g, tr))
	}
	sampled, err := s.Decide(parts)
	require.NoError(t, err)

	var rows []Stored
	want := map[[16]byte]Outcome{}
	var lostErr, split, twoGateways Trace
	for _, tr := range traces {
		switch {
		case tr.Error && lostErr.ID == [16]byte{}:
			lostErr = tr // the error span never arrives: lost
			rows = append(rows, stored(tr, "gw-1", func(sp SpanPlan) bool { return !sp.Error })...)
			want[tr.ID] = OutcomeLost
		case tr.Error && split.ID == [16]byte{} && !okHalfSampled(t, s, g, tr):
			split = tr // only the half with the error span is stored; the other half was dropped
			rows = append(rows, stored(tr, "gw-1", func(sp SpanPlan) bool { return sp.Error || sp.Root })...)
			want[tr.ID] = OutcomeSplit
		case tr.Error && twoGateways.ID == [16]byte{}:
			twoGateways = tr // complete, but two gateways processed it
			rows = append(rows, stored(tr, "gw-1", func(sp SpanPlan) bool { return sp.Root })...)
			rows = append(rows, stored(tr, "gw-2", func(sp SpanPlan) bool { return !sp.Root })...)
			want[tr.ID] = OutcomeSplit
		case sampled[tr.ID]:
			rows = append(rows, stored(tr, "gw-1", func(SpanPlan) bool { return true })...)
			want[tr.ID] = OutcomeStored
		default:
			want[tr.ID] = OutcomeDropped
		}
	}
	require.NotEqual(t, [16]byte{}, split.ID, "the test needs a trace whose ok half is dropped")

	results, err := Account(g, traces, rows, s)
	require.NoError(t, err)
	require.Len(t, results, len(traces))
	for _, r := range results {
		assert.Equal(t, want[r.ID], r.Outcome, "trace %x", r.ID)
	}
	byID := map[[16]byte]Result{}
	for _, r := range results {
		byID[r.ID] = r
	}
	assert.Equal(t, 1, byID[lostErr.ID].LostSpans)
	assert.Equal(t, 2, byID[twoGateways.ID].Gateways)
	assert.Positive(t, byID[split.ID].DroppedSpans)
	assert.Zero(t, byID[split.ID].LostSpans)
}

// okHalfSampled reports whether the sampler would keep tr without its root and error span:
// the half a split leaves on the other gateway.
func okHalfSampled(t *testing.T, s *TailSampler, g *Generator, tr Trace) bool {
	t.Helper()
	half := ptrace.NewTraces()
	for _, b := range g.Build(tr, 1_800_000_000_000_000_000) {
		span := b.Traces.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0)
		if span.ParentSpanID().IsEmpty() || span.Status().Code() == ptrace.StatusCodeError {
			continue
		}
		b.Traces.ResourceSpans().MoveAndAppendTo(half.ResourceSpans())
	}
	sampled, err := s.Decide([]ptrace.Traces{half})
	require.NoError(t, err)
	return sampled[tr.ID]
}

func TestPhases(t *testing.T) {
	windows := []Window{{Name: "scale 2->4", From: 100, To: 200}, {Name: "scale 4->2", From: 400, To: 500}}
	assert.Equal(t, "steady", Phase(windows, 10, 20))
	assert.Equal(t, "scale 2->4", Phase(windows, 90, 20), "a trace still running when the window opens")
	assert.Equal(t, "scale 2->4", Phase(windows, 150, 10))
	assert.Equal(t, "steady", Phase(windows, 250, 100))
	assert.Equal(t, "scale 4->2", Phase(windows, 450, 1000))
}
