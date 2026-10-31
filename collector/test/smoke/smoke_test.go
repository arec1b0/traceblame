package smoke

import (
	"context"
	"encoding/json"
	"os"
	"regexp"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/confmap"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
	"github.com/arec1b0/traceblame/collector/processor/normalizer"
)

const normalizerSettings = `
    tenant_id: smoke-tenant`

// inProcess normalizes td with the library, as the distribution should.
func inProcess(t *testing.T, td ptrace.Traces) ptrace.Traces {
	t.Helper()
	f := normalizer.NewFactory()
	cfg := f.CreateDefaultConfig()
	require.NoError(t, confmap.NewFromStringMap(map[string]any{"tenant_id": "smoke-tenant"}).Unmarshal(cfg))
	sink := new(consumertest.TracesSink)
	p, err := f.CreateTraces(context.Background(), processortest.NewNopSettings(f.Type()), cfg, sink)
	require.NoError(t, err)
	require.NoError(t, p.Start(context.Background(), componenttest.NewNopHost()))
	require.NoError(t, p.ConsumeTraces(context.Background(), td))
	require.NoError(t, p.Shutdown(context.Background()))
	return sink.AllTraces()[0]
}

func TestTheDistributionNormalizesLikeTheLibrary(t *testing.T) {
	c := startCollector(t, normalizerSettings, "info")
	want := map[string]string{}
	for _, fx := range fixtures.All(t) {
		raw, err := os.ReadFile(fx.Path)
		require.NoError(t, err)
		c.post(t, raw)
		for k, v := range spanDocs(t, inProcess(t, fx.Load(t))) {
			want[k] = v
		}
	}
	assert.Contains(t, c.metrics(t), "otelcol_receiver_accepted_spans")

	got := spanDocs(t, c.exported(t)...)
	require.Len(t, got, len(want))
	for k, w := range want {
		assert.JSONEq(t, w, got[k], k)
	}
}

func TestNormalizerCountersUseTheirDesignNames(t *testing.T) {
	var td ptrace.Traces
	for _, fx := range fixtures.All(t) {
		if fx.ID == "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental" {
			td = fx.Load(t)
		}
	}
	require.Equal(t, 1, td.SpanCount())
	ss := td.ResourceSpans().At(0).ScopeSpans().At(0)
	ss.SetSchemaUrl("https://opentelemetry.io/schemas/1.44.0")
	attrs := ss.Spans().At(0).Attributes()
	attrs.PutStr("smoke.unmapped", "x")
	attrs.PutStr("gen_ai.usage.output_tokens", "many")
	body, err := (&ptrace.JSONMarshaler{}).MarshalTraces(td)
	require.NoError(t, err)

	c := startCollector(t, normalizerSettings, "info")
	c.post(t, body)
	metrics := c.metrics(t)
	for _, line := range []string{
		`traceblame_normalizer_unmapped_attrs_total\{[^}]*attr="smoke\.unmapped"[^}]*sdk="otel-genai"[^}]*\} 1`,
		`traceblame_normalizer_newer_schema_spans_total\{[^}]*schema_url="https://opentelemetry\.io/schemas/1\.44\.0"[^}]*sdk="otel-genai"[^}]*\} 1`,
		`traceblame_normalizer_invalid_attrs_total\{[^}]*attr="gen_ai\.usage\.output_tokens"[^}]*sdk="otel-genai"[^}]*\} 1`,
	} {
		assert.Regexp(t, regexp.MustCompile(`(?m)^`+line+`$`), metrics)
	}
	assert.NotContains(t, metrics, "otelcol_traceblame_", "no otelcol_ prefix (design §1.5 names)")
	assert.NotContains(t, metrics, "_total_total")
}

// What content capture puts into the *_content fixtures (tools/fixtures/capture.py prompts and
// answers), and what the captured errors carry: tracebacks with local paths, and the
// server's message that echoes the request ("[] is too short - 'messages'").
var leaks = []string{"France", "Paris", "weather", "Traceback", "/home/", "too short"}

func TestNoContentLeavesTheDistribution(t *testing.T) {
	c := startCollector(t, normalizerSettings, "debug")
	posted := 0
	for _, fx := range fixtures.All(t) {
		if !strings.HasSuffix(fx.ID, "_content") && !strings.Contains(fx.ID, "/chat_error") {
			continue
		}
		raw, err := os.ReadFile(fx.Path)
		require.NoError(t, err)
		found := false
		for _, l := range leaks {
			found = found || strings.Contains(string(raw), l)
		}
		require.True(t, found, "%s carries nothing to leak", fx.ID)
		c.post(t, raw)
		posted++
	}
	require.GreaterOrEqual(t, posted, 8)

	out := c.exportedText(t)
	require.NotEmpty(t, out)
	logs := strings.Join(c.logLines(t), "\n")
	for _, l := range leaks {
		assert.NotContains(t, out, l, "exported spans")
		assert.NotContains(t, logs, l, "collector logs")
	}
}

func TestLogsAreJSON(t *testing.T) {
	c := startCollector(t, normalizerSettings, "info")
	lines := c.logLines(t)
	require.NotEmpty(t, lines)
	for _, line := range lines {
		var entry map[string]any
		assert.NoError(t, json.Unmarshal([]byte(line), &entry), line)
	}
}
