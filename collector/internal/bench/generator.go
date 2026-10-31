// Package bench generates deterministic load from the golden fixtures and accounts for
// every span of it after a run (bench/gateway-scaling, issue #10).
package bench

import (
	"encoding/binary"
	"fmt"
	"math/rand/v2"
	"sort"
	"strings"
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

// ServiceSDK maps each bench service to the SDK whose fixture spans it replays.
var ServiceSDK = map[string]string{
	"bench-openinference": "openinference",
	"bench-openllmetry":   "openllmetry",
	"bench-otel-genai":    "otel-genai",
}

var services = []string{"bench-openinference", "bench-openllmetry", "bench-otel-genai"}

// Params fix a load. Generation is a pure function of Params and the sequence number.
type Params struct {
	RunID         uint32
	Pod           uint8
	SpansPerTrace int // root + SpansPerTrace-1 children
	ErrorEvery    int // every ErrorEvery-th trace (seq % ErrorEvery == ErrorEvery-1) has an error span
	Seed          uint64
}

// SpanPlan is one span of a trace, relative to the trace start.
type SpanPlan struct {
	SpanID   [8]byte
	Parent   [8]byte
	Root     bool
	Error    bool
	Scope    string
	Fixture  string // fixture ID the span copies; "" for the root
	Offset   int64  // ns after the trace start
	Duration int64  // ns
	template ptrace.Span
	scope    pcommon.InstrumentationScope
}

// Trace is one generated trace.
type Trace struct {
	ID       [16]byte
	Service  string
	Error    bool
	Duration int64 // ns, the root span
	Spans    []SpanPlan
}

type pooled struct {
	id    string
	span  ptrace.Span
	scope pcommon.InstrumentationScope
	err   bool
}

// Generator builds traces from real fixture spans (content-off fixtures only).
type Generator struct {
	p     Params
	pools map[string][]pooled // sdk -> spans
}

// NewGenerator loads the content-off fixtures under root.
func NewGenerator(p Params, root string) (*Generator, error) {
	if p.SpansPerTrace < 2 || p.ErrorEvery < 1 {
		return nil, fmt.Errorf("bench: SpansPerTrace >= 2 and ErrorEvery >= 1 required")
	}
	all, err := fixtures.Discover(root)
	if err != nil {
		return nil, err
	}
	g := &Generator{p: p, pools: map[string][]pooled{}}
	for _, fx := range all {
		if strings.HasSuffix(fx.ID, "_content") {
			continue
		}
		td, err := fx.Read()
		if err != nil {
			return nil, err
		}
		ss := td.ResourceSpans().At(0).ScopeSpans().At(0)
		span := ptrace.NewSpan()
		ss.Spans().At(0).CopyTo(span)
		scope := pcommon.NewInstrumentationScope()
		ss.Scope().CopyTo(scope)
		g.pools[fx.SDK()] = append(g.pools[fx.SDK()], pooled{
			id: fx.ID, span: span, scope: scope, err: span.Status().Code() == ptrace.StatusCodeError,
		})
	}
	for _, sdk := range ServiceSDK {
		ok, bad := 0, 0
		for _, s := range g.pools[sdk] {
			if s.err {
				bad++
			} else {
				ok++
			}
		}
		if ok == 0 || bad == 0 {
			return nil, fmt.Errorf("bench: %s needs ok and error fixture spans", sdk)
		}
	}
	return g, nil
}

// TraceID layout: run id (4 bytes), class (1: error), pod, sequence (4), then random bytes.
type TraceID struct {
	RunID uint32
	Error bool
	Pod   uint8
	Seq   uint32
}

// ParseTraceID reads a bench trace id.
func ParseTraceID(id [16]byte) TraceID {
	return TraceID{
		RunID: binary.BigEndian.Uint32(id[0:4]),
		Error: id[4] == 1,
		Pod:   id[5],
		Seq:   binary.BigEndian.Uint32(id[6:10]),
	}
}

// Trace returns trace number seq.
func (g *Generator) Trace(seq uint32) Trace {
	// Deterministic on purpose: the analysis regenerates every trace from the seed.
	rng := rand.New(rand.NewPCG(g.p.Seed^uint64(g.p.RunID)<<32^uint64(g.p.Pod)<<24, uint64(seq))) //nolint:gosec // bench traffic, not a secret
	t := Trace{
		Service: services[int(seq)%len(services)],
		Error:   int(seq)%g.p.ErrorEvery == g.p.ErrorEvery-1,
	}
	binary.BigEndian.PutUint32(t.ID[0:4], g.p.RunID)
	if t.Error {
		t.ID[4] = 1
	}
	t.ID[5] = g.p.Pod
	binary.BigEndian.PutUint32(t.ID[6:10], seq)
	fill(rng, t.ID[10:])

	pool := g.pools[ServiceSDK[t.Service]]
	var ok, bad []pooled
	for _, s := range pool {
		if s.err {
			bad = append(bad, s)
		} else {
			ok = append(ok, s)
		}
	}
	children := g.p.SpansPerTrace - 1
	errAt := -1
	if t.Error {
		errAt = rng.IntN(children)
	}
	root := SpanPlan{Root: true, Scope: "bench.loadgen"}
	fill(rng, root.SpanID[:])
	// Children run one after another, 1 ms apart, each with the fixture span's duration
	// scaled by a deterministic jitter of +-20% (real durations are a few discrete values).
	offset := int64(1_000_000)
	for i := 0; i < children; i++ {
		src := ok[rng.IntN(len(ok))]
		if i == errAt {
			src = bad[rng.IntN(len(bad))]
		}
		base := src.span.EndTimestamp().AsTime().Sub(src.span.StartTimestamp().AsTime()).Nanoseconds()
		dur := int64(float64(base) * (0.8 + 0.4*rng.Float64()))
		s := SpanPlan{
			Parent: root.SpanID, Error: src.err, Scope: src.scope.Name(), Fixture: src.id,
			Offset: offset, Duration: dur, template: src.span, scope: src.scope,
		}
		fill(rng, s.SpanID[:])
		t.Spans = append(t.Spans, s)
		offset += dur + 1_000_000
	}
	root.Duration = offset
	t.Duration = offset
	t.Spans = append([]SpanPlan{root}, t.Spans...)
	return t
}

// fill writes random bytes, never all zero (an all-zero id is invalid).
func fill(rng *rand.Rand, b []byte) {
	for {
		v := rng.Uint64()
		for i := range b {
			b[i] = byte(v >> (8 * (i % 8))) //nolint:gosec // byte i%8 of v, truncation intended
			if i%8 == 7 {
				v = rng.Uint64()
			}
		}
		for _, x := range b {
			if x != 0 {
				return
			}
		}
	}
}

// Batch is one span to export at SendAt (unix ns): spans are exported when they end.
type Batch struct {
	SendAt int64
	Traces ptrace.Traces
}

// Build lays the trace out from start (unix ns) and returns one export per span, ordered
// by send time; the root ends last.
func (g *Generator) Build(t Trace, start int64) []Batch {
	out := make([]Batch, 0, len(t.Spans))
	for _, s := range t.Spans {
		td := ptrace.NewTraces()
		rs := td.ResourceSpans().AppendEmpty()
		rs.Resource().Attributes().PutStr("service.name", t.Service)
		ss := rs.ScopeSpans().AppendEmpty()
		span := ss.Spans().AppendEmpty()
		if s.Root {
			ss.Scope().SetName(s.Scope)
			span.SetName("bench.request")
			span.SetKind(ptrace.SpanKindServer)
		} else {
			s.scope.CopyTo(ss.Scope())
			s.template.CopyTo(span)
			span.SetParentSpanID(s.Parent)
		}
		span.SetTraceID(t.ID)
		span.SetSpanID(s.SpanID)
		begin := start + s.Offset
		span.SetStartTimestamp(pcommon.NewTimestampFromTime(time.Unix(0, begin)))
		span.SetEndTimestamp(pcommon.NewTimestampFromTime(time.Unix(0, begin+s.Duration)))
		out = append(out, Batch{SendAt: begin + s.Duration, Traces: td})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].SendAt < out[j].SendAt })
	return out
}

// P95 returns the 95th percentile of the trace duration per service over traces 0..n-1,
// rounded up to the millisecond, in ns.
func (g *Generator) P95(n uint32) map[string]int64 {
	by := map[string][]int64{}
	for seq := uint32(0); seq < n; seq++ {
		t := g.Trace(seq)
		by[t.Service] = append(by[t.Service], t.Duration)
	}
	out := map[string]int64{}
	for svc, d := range by {
		sort.Slice(d, func(i, j int) bool { return d[i] < d[j] })
		v := d[(len(d)*95+99)/100-1]
		out[svc] = (v + 999_999) / 1_000_000 * 1_000_000
	}
	return out
}
