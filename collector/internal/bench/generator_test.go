package bench

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

func generator(t *testing.T, p Params) *Generator {
	t.Helper()
	g, err := NewGenerator(p, fixtures.Root())
	require.NoError(t, err)
	return g
}

var params = Params{RunID: 0xbe57, Pod: 1, SpansPerTrace: 4, ErrorEvery: 2, Seed: 7}

func TestTraceIDsEncodeRunClassPodAndSequence(t *testing.T) {
	g := generator(t, params)
	for _, seq := range []uint32{0, 1, 7, 1 << 20} {
		tr := g.Trace(seq)
		id := ParseTraceID(tr.ID)
		assert.Equal(t, params.RunID, id.RunID)
		assert.Equal(t, params.Pod, id.Pod)
		assert.Equal(t, seq, id.Seq)
		assert.Equal(t, seq%2 == 1, id.Error, "every second trace is an error trace")
		assert.Equal(t, tr.Error, id.Error)
	}
}

func TestGenerationIsDeterministic(t *testing.T) {
	a, b := generator(t, params), generator(t, params)
	for seq := uint32(0); seq < 50; seq++ {
		x, y := a.Trace(seq), b.Trace(seq)
		assert.Equal(t, x.ID, y.ID)
		assert.Equal(t, x.Duration, y.Duration)
		assert.Equal(t, len(x.Spans), len(y.Spans))
		for i := range x.Spans {
			assert.Equal(t, x.Spans[i].SpanID, y.Spans[i].SpanID)
			assert.Equal(t, x.Spans[i].Offset, y.Spans[i].Offset)
		}
	}
	assert.NotEqual(t, a.Trace(1).ID, a.Trace(2).ID)
}

func TestTracesReplayRealFixtureSpansOfTheirService(t *testing.T) {
	g := generator(t, params)
	services := map[string]bool{}
	for seq := uint32(0); seq < 30; seq++ {
		tr := g.Trace(seq)
		services[tr.Service] = true
		require.Len(t, tr.Spans, params.SpansPerTrace)
		root := tr.Spans[0]
		assert.True(t, root.Root)
		assert.Equal(t, "bench.loadgen", root.Scope)
		errors := 0
		for _, s := range tr.Spans[1:] {
			assert.False(t, s.Root)
			assert.Equal(t, root.SpanID, s.Parent)
			assert.NotEmpty(t, s.Fixture, "children are copies of golden fixture spans")
			assert.Contains(t, s.Fixture, ServiceSDK[tr.Service]+"/", "spans come from the service's SDK")
			assert.GreaterOrEqual(t, s.Offset, int64(0))
			assert.LessOrEqual(t, s.Offset+s.Duration, root.Duration, "the root covers its children")
			if s.Error {
				errors++
			}
		}
		if tr.Error {
			assert.Equal(t, 1, errors, "an error trace carries exactly one real error span")
		} else {
			assert.Zero(t, errors)
		}
		assert.Equal(t, root.Duration, tr.Duration)
	}
	assert.Len(t, services, 3)
}

func TestBuildPlacesSpansInTimeAndSendsEachAtItsEnd(t *testing.T) {
	g := generator(t, params)
	tr := g.Trace(3)
	start := int64(1_800_000_000_000_000_000)
	batches := g.Build(tr, start)
	sends := 0
	for _, b := range batches {
		sends += b.Traces.SpanCount()
		rs := b.Traces.ResourceSpans().At(0)
		svc, _ := rs.Resource().Attributes().Get("service.name")
		assert.Equal(t, tr.Service, svc.Str())
		span := rs.ScopeSpans().At(0).Spans().At(0)
		assert.Equal(t, b.SendAt, span.EndTimestamp().AsTime().UnixNano(), "a span is exported when it ends")
		assert.Equal(t, tr.ID, [16]byte(span.TraceID()))
	}
	assert.Equal(t, len(tr.Spans), sends)
	last := batches[len(batches)-1]
	root := last.Traces.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0)
	assert.True(t, root.ParentSpanID().IsEmpty(), "the root ends last")
	assert.Equal(t, ptrace.SpanKindServer, root.Kind())
}

func TestP95PerService(t *testing.T) {
	g := generator(t, params)
	p95 := g.P95(3000)
	assert.Len(t, p95, 3)
	for svc, v := range p95 {
		durations := []int64{}
		for seq := uint32(0); seq < 3000; seq++ {
			if tr := g.Trace(seq); tr.Service == svc {
				durations = append(durations, tr.Duration)
			}
		}
		above := 0
		for _, d := range durations {
			if d > v {
				above++
			}
		}
		share := float64(above) / float64(len(durations))
		assert.InDelta(t, 0.05, share, 0.02, "%s: about 5%% of the traces are slower than its p95", svc)
	}
}
