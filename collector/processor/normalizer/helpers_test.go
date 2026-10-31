package normalizer

import (
	"context"
	"testing"

	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/profile"
)

const testTenant = "fixture-tenant"

// harness runs one normalizer over pdata and keeps its telemetry and logs.
type harness struct {
	t      *testing.T
	sink   *consumertest.TracesSink
	tel    *componenttest.Telemetry
	logger *zap.Logger
	logs   *observer.ObservedLogs
	cfg    *Config
}

func newHarness(t *testing.T, mutate func(*Config)) *harness {
	t.Helper()
	cfg := createDefaultConfig().(*Config)
	cfg.TenantID = testTenant
	if mutate != nil {
		mutate(cfg)
	}
	require.NoError(t, cfg.Validate())
	core, logs := observer.New(zap.DebugLevel)
	tel := componenttest.NewTelemetry()
	t.Cleanup(func() { require.NoError(t, tel.Shutdown(context.Background())) })
	return &harness{
		t: t, sink: new(consumertest.TracesSink), tel: tel,
		logger: zap.New(core), logs: logs, cfg: cfg,
	}
}

// run normalizes td and returns the single export the processor forwarded.
func (h *harness) run(td ptrace.Traces) ptrace.Traces {
	h.t.Helper()
	set := processortest.NewNopSettings(componentType)
	set.TelemetrySettings = h.tel.NewTelemetrySettings()
	set.Logger = h.logger
	p, err := NewFactory().CreateTraces(context.Background(), set, h.cfg, h.sink)
	require.NoError(h.t, err)
	require.NoError(h.t, p.Start(context.Background(), componenttest.NewNopHost()))
	defer func() { require.NoError(h.t, p.Shutdown(context.Background())) }()
	h.sink.Reset()
	require.NoError(h.t, p.ConsumeTraces(context.Background(), td))
	got := h.sink.AllTraces()
	require.Len(h.t, got, 1)
	return got[0]
}

// counts returns the data points of a normalizer counter as "label1|label2" -> value.
func (h *harness) counts(name, label1, label2 string) map[string]int64 {
	h.t.Helper()
	out := map[string]int64{}
	m, err := h.tel.GetMetric(name)
	if err != nil {
		return out // never recorded
	}
	sum, ok := m.Data.(metricdata.Sum[int64])
	require.True(h.t, ok, "%s is not an int64 sum", name)
	require.True(h.t, sum.IsMonotonic, "%s is not a counter", name)
	for _, dp := range sum.DataPoints {
		v1, _ := dp.Attributes.Value(attribute.Key(label1))
		v2, _ := dp.Attributes.Value(attribute.Key(label2))
		out[v1.AsString()+"|"+v2.AsString()] += dp.Value
	}
	return out
}

func fixtureByID(t *testing.T, id string) fixtures.Fixture {
	t.Helper()
	for _, fx := range fixtures.All(t) {
		if fx.ID == id {
			return fx
		}
	}
	t.Fatalf("no fixture %s", id)
	return fixtures.Fixture{}
}

// eachSpan calls fn for every span with its resource and scope.
func eachSpan(td ptrace.Traces, fn func(res pcommon.Resource, ss ptrace.ScopeSpans, span ptrace.Span)) {
	for i := 0; i < td.ResourceSpans().Len(); i++ {
		rs := td.ResourceSpans().At(i)
		for j := 0; j < rs.ScopeSpans().Len(); j++ {
			ss := rs.ScopeSpans().At(j)
			for k := 0; k < ss.Spans().Len(); k++ {
				fn(rs.Resource(), ss, ss.Spans().At(k))
			}
		}
	}
}

func (h *harness) profileAttr(name string) (*profile.Attribute, bool) {
	h.t.Helper()
	p, err := loadProfile(h.cfg.Profile)
	require.NoError(h.t, err)
	return p.Attribute(name)
}
