package normalizer

import (
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

const (
	unmappedMetric    = "traceblame_normalizer_unmapped_attrs_total"
	newerSchemaMetric = "traceblame_normalizer_newer_schema_spans_total"
	invalidMetric     = "traceblame_normalizer_invalid_attrs_total"
)

type want struct {
	sdk    string
	kind   string
	attrs  map[string]any
	absent []string
}

func llm(model string, extra map[string]any) map[string]any {
	out := map[string]any{
		"gen_ai.operation.name": "chat",
		"gen_ai.provider.name":  "openai",
		"gen_ai.request.model":  model,
	}
	for k, v := range extra {
		out[k] = v
	}
	return out
}

func embedding(extra map[string]any) map[string]any {
	out := map[string]any{
		"gen_ai.operation.name":     "embeddings",
		"gen_ai.provider.name":      "openai",
		"gen_ai.request.model":      "nomic-embed-text",
		"gen_ai.response.model":     "nomic-embed-text",
		"gen_ai.usage.input_tokens": int64(9),
	}
	for k, v := range extra {
		out[k] = v
	}
	return out
}

var (
	chatBasic = map[string]any{
		"gen_ai.response.model":          "qwen2.5:7b",
		"gen_ai.request.max_tokens":      int64(16),
		"gen_ai.usage.input_tokens":      int64(38),
		"gen_ai.usage.output_tokens":     int64(2),
		"gen_ai.response.finish_reasons": []any{"stop"},
	}
	chatToolCall = map[string]any{
		"gen_ai.response.model":          "qwen2.5:7b",
		"gen_ai.request.max_tokens":      int64(64),
		"gen_ai.usage.input_tokens":      int64(155),
		"gen_ai.usage.output_tokens":     int64(20),
		"gen_ai.response.finish_reasons": []any{"tool_calls"},
	}
	localhost = map[string]any{"server.address": "localhost"}
)

func merge(ms ...map[string]any) map[string]any {
	out := map[string]any{}
	for _, m := range ms {
		for k, v := range m {
			out[k] = v
		}
	}
	return out
}

// One expectation per golden fixture; a fixture without one fails the test.
var fixtureWants = map[string]want{
	"openinference/openinference-instrumentation-openai-0.1.68/chat_basic": {
		sdk: "openinference", kind: "llm", attrs: llm("qwen2.5:7b", chatBasic),
		absent: []string{"llm.system", "llm.model_name", "llm.token_count.prompt", "llm.token_count.completion", "llm.finish_reason", "error.type"},
	},
	"openinference/openinference-instrumentation-openai-0.1.68/chat_tool_call": {
		sdk: "openinference", kind: "llm", attrs: llm("qwen2.5:7b", chatToolCall),
	},
	"openinference/openinference-instrumentation-openai-0.1.68/chat_error": {
		sdk: "openinference", kind: "llm",
		attrs:  llm("qwen2.5:7b", map[string]any{"gen_ai.request.max_tokens": int64(16), "error.type": "openai.BadRequestError"}),
		absent: []string{"gen_ai.response.model", "gen_ai.usage.input_tokens"},
	},
	"openinference/openinference-instrumentation-openai-0.1.68/embeddings_basic": {
		sdk: "openinference", kind: "embedding", attrs: embedding(nil),
		absent: []string{"embedding.model_name", "llm.system", "llm.token_count.prompt"},
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic": {
		sdk: "openllmetry", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatBasic, localhost)),
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_tool_call": {
		sdk: "openllmetry", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatToolCall, localhost)),
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_error": {
		sdk: "openllmetry", kind: "llm",
		attrs: llm("qwen2.5:7b", merge(localhost, map[string]any{"gen_ai.request.max_tokens": int64(16), "error.type": "BadRequestError"})),
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/embeddings_basic": {
		sdk: "openllmetry", kind: "embedding", attrs: embedding(localhost),
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic": {
		sdk: "otel-genai", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatBasic, localhost)),
		absent: []string{"gen_ai.system"},
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_tool_call": {
		sdk: "otel-genai", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatToolCall, localhost)),
		absent: []string{"gen_ai.system"},
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_error": {
		sdk: "otel-genai", kind: "llm",
		attrs:  llm("qwen2.5:7b", merge(localhost, map[string]any{"gen_ai.request.max_tokens": int64(16), "error.type": "BadRequestError"})),
		absent: []string{"gen_ai.system"},
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/embeddings_basic": {
		sdk: "otel-genai", kind: "embedding", attrs: embedding(localhost), absent: []string{"gen_ai.system"},
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental": {
		sdk: "otel-genai", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatBasic, localhost)),
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_tool_call_latest_experimental": {
		sdk: "otel-genai", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatToolCall, localhost)),
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_error_latest_experimental": {
		sdk: "otel-genai", kind: "llm",
		attrs: llm("qwen2.5:7b", merge(localhost, map[string]any{"gen_ai.request.max_tokens": int64(16), "error.type": "BadRequestError"})),
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/embeddings_basic_latest_experimental": {
		sdk: "otel-genai", kind: "embedding", attrs: embedding(localhost),
	},
	// Content capture on (issue #9): the same mapping, and the content is gone.
	"openinference/openinference-instrumentation-openai-0.1.68/chat_basic_content": {
		sdk: "openinference", kind: "llm", attrs: llm("qwen2.5:7b", chatBasic),
		absent: []string{"input.value", "output.value", "llm.input_messages.0.message.content", "llm.output_messages.0.message.content", "llm.invocation_parameters"},
	},
	"openinference/openinference-instrumentation-openai-0.1.68/chat_tool_call_content": {
		sdk: "openinference", kind: "llm", attrs: llm("qwen2.5:7b", chatToolCall),
		absent: []string{"llm.tools.0.tool.json_schema", "llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments"},
	},
	"openinference/openinference-instrumentation-openai-0.1.68/embeddings_basic_content": {
		sdk: "openinference", kind: "embedding", attrs: embedding(nil),
		absent: []string{"embedding.embeddings.0.embedding.text", "embedding.embeddings.0.embedding.vector", "embedding.invocation_parameters"},
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic_content": {
		sdk: "openllmetry", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatBasic, localhost)),
		absent: []string{"gen_ai.input.messages", "gen_ai.output.messages"},
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_tool_call_content": {
		sdk: "openllmetry", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatToolCall, localhost)),
		absent: []string{"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.tool.definitions"},
	},
	"openllmetry/opentelemetry-instrumentation-openai-0.62.4/embeddings_basic_content": {
		sdk: "openllmetry", kind: "embedding", attrs: embedding(localhost),
		absent: []string{"gen_ai.input.messages"},
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental_content": {
		sdk: "otel-genai", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatBasic, localhost)),
		absent: []string{"gen_ai.input.messages", "gen_ai.output.messages"},
	},
	"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_tool_call_latest_experimental_content": {
		sdk: "otel-genai", kind: "llm", attrs: llm("qwen2.5:7b", merge(chatToolCall, localhost)),
		absent: []string{"gen_ai.input.messages", "gen_ai.output.messages"},
	},
}

func TestEveryFixtureHasAnExpectation(t *testing.T) {
	ids := map[string]bool{}
	for _, fx := range fixtures.All(t) {
		ids[fx.ID] = true
		assert.Contains(t, fixtureWants, fx.ID, "add an expectation for the new fixture")
	}
	for id := range fixtureWants {
		assert.True(t, ids[id], "expectation for a fixture that does not exist: %s", id)
	}
}

func TestNormalizesEveryFixture(t *testing.T) {
	for _, fx := range fixtures.All(t) {
		w, ok := fixtureWants[fx.ID]
		if !ok {
			continue // reported by TestEveryFixtureHasAnExpectation
		}
		t.Run(fx.ID, func(t *testing.T) {
			h := newHarness(t, nil)
			out := h.run(fx.Load(t))
			spans := 0
			eachSpan(out, func(res pcommon.Resource, _ ptrace.ScopeSpans, span ptrace.Span) {
				spans++
				tenant, ok := res.Attributes().Get("aegis.tenant.id")
				require.True(t, ok)
				assert.Equal(t, testTenant, tenant.Str())

				got := span.Attributes().AsRaw()
				assert.Equal(t, "aegis-telemetry/1", got["aegis.profile.version"])
				assert.Equal(t, Version, got["aegis.normalizer.version"])
				assert.Equal(t, w.sdk, got["aegis.source.sdk"])
				assert.Equal(t, w.kind, got["aegis.span.kind"])
				for k, v := range w.attrs {
					assert.Equal(t, v, got[k], k)
				}
				for _, k := range w.absent {
					assert.NotContains(t, got, k)
				}
				for k := range got {
					if strings.HasPrefix(k, "aegis.") {
						_, isProfile := h.profileAttr(k)
						assert.True(t, isProfile, "unknown aegis attribute %s", k)
					}
				}
			})
			assert.Equal(t, 1, spans)
			// Every attribute of every real fixture is mapped or known to its table.
			assert.Empty(t, h.counts(unmappedMetric, "sdk", "attr"))
			assert.Empty(t, h.counts(newerSchemaMetric, "sdk", "schema_url"))
			assert.Empty(t, h.counts(invalidMetric, "sdk", "attr"))
		})
	}
}

// firstSpan is the only span of a single-span fixture.
func firstSpan(td ptrace.Traces) ptrace.Span {
	return td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0)
}

func TestUnmappedAttributesPassThroughAndAreCounted(t *testing.T) {
	td := fixtureByID(t, "openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic").Load(t)
	attrs := firstSpan(td).Attributes()
	attrs.PutStr("gen_ai.request.reasoning_effort", "low")
	attrs.PutStr("app.retries.0.reason", "timeout")
	attrs.PutStr("app.retries.1.reason", "timeout")

	h := newHarness(t, nil)
	got := firstSpan(h.run(td)).Attributes().AsRaw()
	assert.Equal(t, "low", got["gen_ai.request.reasoning_effort"])
	assert.Contains(t, got, "app.retries.1.reason")
	assert.Equal(t, map[string]int64{
		"openllmetry|gen_ai.request.reasoning_effort": 1,
		"openllmetry|app.retries.*.reason":            2,
	}, h.counts(unmappedMetric, "sdk", "attr"))

	// The first sighting of each label set is logged with the trace id, never the value.
	entries := h.logs.FilterMessage("unmapped attribute").All()
	require.Len(t, entries, 2)
	for _, e := range entries {
		fields := e.ContextMap()
		assert.Equal(t, "openllmetry", fields["sdk"])
		assert.Equal(t, Version, fields["normalizer_version"])
		assert.Regexp(t, `^[0-9a-f]{32}$`, fields["trace_id"])
		assert.NotContains(t, fields, "value")
	}
}

func TestUnmappedCountingIsCappedPerCounter(t *testing.T) {
	td := fixtureByID(t, "openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic").Load(t)
	attrs := firstSpan(td).Attributes()
	for _, k := range []string{"x.a", "x.b", "x.c", "x.d"} {
		attrs.PutStr(k, "v")
	}
	h := newHarness(t, func(c *Config) { c.MaxLabelValues = 2 })
	h.run(td)
	counts := h.counts(unmappedMetric, "sdk", "attr")
	assert.Len(t, counts, 3)
	assert.Equal(t, int64(2), counts["openllmetry|_other"])
}

func TestNewerSchemaURLMapsWithTheNewestTableAndIsCounted(t *testing.T) {
	for _, url := range []string{
		"https://opentelemetry.io/schemas/1.44.0",
		"https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev",
	} {
		t.Run(url, func(t *testing.T) {
			td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental").Load(t)
			td.ResourceSpans().At(0).ScopeSpans().At(0).SetSchemaUrl(url)
			h := newHarness(t, nil)
			out := h.run(td)
			assert.Equal(t, 1, out.SpanCount(), "a span is never dropped for its schema_url")
			assert.Equal(t, "otel-genai", firstSpan(out).Attributes().AsRaw()["aegis.source.sdk"])
			assert.Equal(t, map[string]int64{"otel-genai|" + url: 1}, h.counts(newerSchemaMetric, "sdk", "schema_url"))
			// openai.response.system_fingerprint is known to the newest (1.37.0) table only.
			assert.Empty(t, h.counts(unmappedMetric, "sdk", "attr"))
		})
	}
}

func TestOlderSchemaTableCountsAttributesOnlyNewerTablesKnow(t *testing.T) {
	td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental").Load(t)
	td.ResourceSpans().At(0).ScopeSpans().At(0).SetSchemaUrl("https://opentelemetry.io/schemas/1.30.0")
	h := newHarness(t, nil)
	h.run(td)
	assert.Equal(t, map[string]int64{"otel-genai|openai.response.system_fingerprint": 1},
		h.counts(unmappedMetric, "sdk", "attr"))
}

func TestUnknownAegisAttributesAreDropped(t *testing.T) {
	td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic").Load(t)
	firstSpan(td).Attributes().PutStr("aegis.secret.flag", "x")
	firstSpan(td).Attributes().PutStr("aegis.tenant.id", "span-scoped") // resource attribute
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("aegis.session.id", "resource-scoped")

	h := newHarness(t, nil)
	out := h.run(td)
	got := firstSpan(out).Attributes().AsRaw()
	assert.NotContains(t, got, "aegis.secret.flag")
	assert.NotContains(t, got, "aegis.tenant.id")
	assert.NotContains(t, out.ResourceSpans().At(0).Resource().Attributes().AsRaw(), "aegis.session.id")
	assert.Equal(t, map[string]int64{
		"otel-genai|aegis.secret.flag": 1,
		"otel-genai|aegis.tenant.id":   1,
		"unknown|aegis.session.id":     1,
	}, h.counts(invalidMetric, "sdk", "attr"))
}

func TestMistypedProfileAttributesAreRepairedOrDropped(t *testing.T) {
	td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic").Load(t)
	attrs := firstSpan(td).Attributes()
	attrs.PutStr("gen_ai.request.max_tokens", "16")
	attrs.PutStr("gen_ai.usage.input_tokens", "many")
	attrs.PutStr("gen_ai.response.finish_reasons", "stop")

	h := newHarness(t, nil)
	got := firstSpan(h.run(td)).Attributes().AsRaw()
	assert.Equal(t, int64(16), got["gen_ai.request.max_tokens"])
	assert.Equal(t, []any{"stop"}, got["gen_ai.response.finish_reasons"])
	assert.NotContains(t, got, "gen_ai.usage.input_tokens")
	assert.Equal(t, map[string]int64{"otel-genai|gen_ai.usage.input_tokens": 1}, h.counts(invalidMetric, "sdk", "attr"))
}

func TestAnUnknownOperationMakesAChainSpanAndIsCounted(t *testing.T) {
	td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic").Load(t)
	firstSpan(td).Attributes().PutStr("gen_ai.operation.name", "generate")
	h := newHarness(t, nil)
	got := firstSpan(h.run(td)).Attributes().AsRaw()
	assert.Equal(t, "chain", got["aegis.span.kind"])
	assert.Equal(t, "generate", got["gen_ai.operation.name"], "never guess a rename")
	assert.Equal(t, map[string]int64{"otel-genai|gen_ai.operation.name": 1}, h.counts(invalidMetric, "sdk", "attr"))
}

func TestSpansOutsideEveryTable(t *testing.T) {
	td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic").Load(t)
	ss := td.ResourceSpans().At(0).ScopeSpans().At(0)
	ss.Scope().SetName("my.app.genai")
	ss.SetSchemaUrl("")

	h := newHarness(t, nil)
	got := firstSpan(h.run(td)).Attributes().AsRaw()
	assert.Equal(t, "unknown", got["aegis.source.sdk"])
	assert.Equal(t, "llm", got["aegis.span.kind"], "the kind still follows gen_ai.operation.name")
	assert.Equal(t, "openai", got["gen_ai.system"], "no table, no rename")
	assert.Empty(t, h.counts(unmappedMetric, "sdk", "attr"), "unknown SDKs are not counted")

	attrs := firstSpan(td).Attributes()
	attrs.Clear()
	attrs.PutStr("http.request.method", "POST")
	h = newHarness(t, nil)
	got = firstSpan(h.run(td)).Attributes().AsRaw()
	assert.Equal(t, "chain", got["aegis.span.kind"])
	assert.Equal(t, "POST", got["http.request.method"])
	assert.Empty(t, h.counts(unmappedMetric, "sdk", "attr"))
}

func TestTenant(t *testing.T) {
	fx := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic")

	td := fx.Load(t)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("aegis.tenant.id", "from-shim")
	out := newHarness(t, nil).run(td)
	v, _ := out.ResourceSpans().At(0).Resource().Attributes().Get("aegis.tenant.id")
	assert.Equal(t, "from-shim", v.Str(), "a tenant set by the shim is kept")

	out = newHarness(t, func(c *Config) { c.TenantID = "" }).run(fx.Load(t))
	_, ok := out.ResourceSpans().At(0).Resource().Attributes().Get("aegis.tenant.id")
	assert.False(t, ok, "no tenant configured, none filled")
}
