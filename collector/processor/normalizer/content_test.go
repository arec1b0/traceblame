package normalizer

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.uber.org/zap/zaptest/observer"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

const droppedLargeMetric = "traceblame_normalizer_dropped_large_values_total"

// needles are words of the capture prompts and answers (tools/fixtures/capture.py): what
// content capture puts into the *_content fixtures, and nothing else in a fixture contains.
var needles = []string{"France", "Paris", "weather"}

// contentKeys are SDK content attributes taken from the SDK conventions, independent of the
// mapping tables: OpenInference, OpenLLMetry and OTel GenAI semconv.
var contentKeys = []string{
	"input.value",
	"output.value",
	"llm.input_messages.0.message.content",
	"llm.output_messages.0.message.content",
	"llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments",
	"llm.prompts.0",
	"llm.prompt_template.template",
	"llm.prompt_template.variables",
	"llm.invocation_parameters",
	"llm.tools.0.tool.json_schema",
	"embedding.embeddings.0.embedding.text",
	"retrieval.documents.0.document.content",
	"reranker.query",
	"tool.parameters",
	"gen_ai.prompt.0.content",
	"gen_ai.completion.0.content",
	"traceloop.entity.input",
	"traceloop.entity.output",
	"gen_ai.input.messages",
	"gen_ai.output.messages",
	"gen_ai.system_instructions",
	"gen_ai.tool.call.arguments",
	"gen_ai.tool.call.result",
	"gen_ai.retrieval.query.text",
	"gen_ai.retrieval.documents",
	"gen_ai.tool.definitions",
	"gen_ai.prompt",
	"gen_ai.completion",
}

// contentLike flags attribute keys shaped like content, independent of the mapping tables.
var contentLike = regexp.MustCompile(
	`(^|\.)(prompts?|completions?|messages?|content|text|vector|arguments|definitions|json_schema|value|query|result)(\.|$)`)

// exported is everything a span leaves the collector with, as OTLP/JSON and protobuf.
func exported(t *testing.T, td ptrace.Traces) string {
	t.Helper()
	j, err := (&ptrace.JSONMarshaler{}).MarshalTraces(td)
	require.NoError(t, err)
	p, err := (&ptrace.ProtoMarshaler{}).MarshalTraces(td)
	require.NoError(t, err)
	return string(j) + string(p)
}

func assertLogsLack(t *testing.T, logs *observer.ObservedLogs, needles ...string) {
	t.Helper()
	for _, e := range logs.All() {
		line := e.Message + " " + fmt.Sprint(e.ContextMap())
		for _, n := range needles {
			assert.NotContains(t, line, n, "log entry %q", e.Message)
		}
	}
}

func assertNoContentLikeKeys(t *testing.T, td ptrace.Traces) {
	t.Helper()
	check := func(where string, m pcommon.Map) {
		m.Range(func(k string, _ pcommon.Value) bool {
			if !strings.HasPrefix(k, "aegis.") {
				assert.NotRegexp(t, contentLike, k, "%s attribute", where)
			}
			return true
		})
	}
	eachSpan(td, func(res pcommon.Resource, _ ptrace.ScopeSpans, span ptrace.Span) {
		check("resource", res.Attributes())
		check("span", span.Attributes())
		for i := 0; i < span.Events().Len(); i++ {
			check("event", span.Events().At(i).Attributes())
		}
		for i := 0; i < span.Links().Len(); i++ {
			check("link", span.Links().At(i).Attributes())
		}
	})
}

func TestNoContentSurvivesAnyFixture(t *testing.T) {
	for _, fx := range fixtures.All(t) {
		t.Run(fx.ID, func(t *testing.T) {
			td := fx.Load(t)
			before := exported(t, td)
			if strings.HasSuffix(fx.ID, "_content") {
				found := false
				for _, n := range needles {
					found = found || strings.Contains(before, n)
				}
				require.True(t, found, "a content fixture must carry content, or this test proves nothing")
			}
			h := newHarness(t, nil)
			out := h.run(td)
			after := exported(t, out)
			for _, n := range needles {
				assert.NotContains(t, after, n)
			}
			assertNoContentLikeKeys(t, out)
			assertLogsLack(t, h.logs, needles...)
			assert.Empty(t, h.counts(droppedLargeMetric, "sdk", "attr"), "every fixture attribute is classified")
		})
	}
}

func TestCanariesInContentAttributesNeverSurvive(t *testing.T) {
	for _, id := range []string{
		"openinference/openinference-instrumentation-openai-0.1.68/chat_basic",
		"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic",
		"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic",
		"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental",
	} {
		t.Run(id, func(t *testing.T) {
			td := fixtureByID(t, id).Load(t)
			canary := func(where string) string { return "tb-canary-" + where }
			span := firstSpan(td)
			for _, k := range contentKeys {
				span.Attributes().PutStr(k, canary(k))
			}
			td.ResourceSpans().At(0).Resource().Attributes().PutStr("gen_ai.system_instructions", canary("resource"))
			ev := span.Events().AppendEmpty()
			ev.SetName("gen_ai.content.prompt")
			ev.Attributes().PutStr("gen_ai.prompt", canary("event"))
			exc := span.Events().AppendEmpty()
			exc.SetName("exception")
			exc.Attributes().PutStr("exception.type", "ValueError")
			exc.Attributes().PutStr("exception.message", canary("exception.message"))
			exc.Attributes().PutStr("exception.stacktrace", canary("exception.stacktrace"))
			span.Status().SetCode(ptrace.StatusCodeError)
			span.Status().SetMessage(canary("status"))
			link := span.Links().AppendEmpty()
			link.Attributes().PutStr("gen_ai.input.messages", canary("link"))
			span.Attributes().PutStr("x.unknown.blob", canary("blob")+strings.Repeat(" padding", 40))

			h := newHarness(t, nil)
			out := h.run(td)
			assert.NotContains(t, exported(t, out), "tb-canary-")
			assertNoContentLikeKeys(t, out)
			assertLogsLack(t, h.logs, "tb-canary-")

			got := firstSpan(out)
			require.Equal(t, 1, got.Events().Len(), "only the exception event stays")
			assert.Equal(t, map[string]any{"exception.type": "ValueError"}, got.Events().At(0).Attributes().AsRaw())
			assert.Equal(t, ptrace.StatusCodeError, got.Status().Code(), "the status code stays")
		})
	}
}

func TestContentRulesApplyToSpansOutsideEveryTable(t *testing.T) {
	td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic").Load(t)
	ss := td.ResourceSpans().At(0).ScopeSpans().At(0)
	ss.Scope().SetName("my.http.client")
	ss.SetSchemaUrl("")
	span := firstSpan(td)
	span.Attributes().Clear()
	span.Attributes().PutStr("http.request.method", "POST")
	span.Attributes().PutStr("input.value", "tb-canary-input")
	span.Status().SetMessage("connection reset")
	ev := span.Events().AppendEmpty()
	ev.SetName("retry")
	ev.Attributes().PutStr("attempt", "2")
	ev.Attributes().PutStr("gen_ai.prompt", "tb-canary-event")
	blob := strings.Repeat("b", 1000)
	span.Attributes().PutStr("http.response.body", blob)

	normalized := newHarness(t, nil).run(td)
	assert.NotContains(t, exported(t, normalized), "tb-canary-")
	out := firstSpan(normalized)
	assert.Equal(t, "chain", out.Attributes().AsRaw()["aegis.span.kind"])
	assert.Equal(t, "connection reset", out.Status().Message(), "non-GenAI spans keep their status message")
	require.Equal(t, 1, out.Events().Len(), "and their events")
	assert.Equal(t, map[string]any{"attempt": "2"}, out.Events().At(0).Attributes().AsRaw())
	assert.Equal(t, blob, out.Attributes().AsRaw()["http.response.body"], "the size guard is for GenAI spans")
}

func TestErrorDetailsKeepOnlyTheExceptionType(t *testing.T) {
	for _, id := range []string{
		"openinference/openinference-instrumentation-openai-0.1.68/chat_error",
		"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_error",
		"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_error",
		"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_error_latest_experimental",
	} {
		t.Run(id, func(t *testing.T) {
			td := fixtureByID(t, id).Load(t)
			require.NotEmpty(t, firstSpan(td).Status().Message(), "the captured error carries a message")
			out := newHarness(t, nil).run(td)
			span := firstSpan(out)
			assert.Equal(t, ptrace.StatusCodeError, span.Status().Code())
			assert.Empty(t, span.Status().Message())
			for i := 0; i < span.Events().Len(); i++ {
				ev := span.Events().At(i)
				assert.Equal(t, "exception", ev.Name())
				assert.Equal(t, []string{"exception.type"}, keys(ev.Attributes()))
			}
			assert.Contains(t, span.Attributes().AsRaw(), "error.type")
			after := exported(t, out)
			for _, leak := range []string{"Traceback", "/home/", "too short"} {
				assert.NotContains(t, after, leak)
			}
		})
	}
}

func keys(m pcommon.Map) []string {
	var out []string
	m.Range(func(k string, _ pcommon.Value) bool {
		out = append(out, k)
		return true
	})
	return out
}

func sha16(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])[:16]
}

func TestToolCallArgumentsBecomeTheArgsHash(t *testing.T) {
	tool := func(args string, mutate func(pcommon.Map)) map[string]any {
		td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental").Load(t)
		attrs := firstSpan(td).Attributes()
		attrs.PutStr("gen_ai.operation.name", "execute_tool")
		attrs.PutStr("gen_ai.tool.name", "get_weather")
		attrs.PutStr("gen_ai.tool.call.arguments", args)
		if mutate != nil {
			mutate(attrs)
		}
		return firstSpan(newHarness(t, nil).run(td)).Attributes().AsRaw()
	}

	got := tool(`{"units": "metric", "city": "Paris"}`, nil)
	assert.Equal(t, "tool", got["aegis.span.kind"])
	assert.Equal(t, sha16(`{"city":"Paris","units":"metric"}`), got["aegis.tool.args_hash"], "RFC 8785 form")
	assert.NotContains(t, got, "gen_ai.tool.call.arguments")

	assert.Equal(t, sha16(`city=Paris`), tool(`city=Paris`, nil)["aegis.tool.args_hash"], "not JSON: raw bytes")
	assert.Equal(t, "0123456789abcdef", tool(`{"city": "Paris"}`, func(m pcommon.Map) {
		m.PutStr("aegis.tool.args_hash", "0123456789abcdef")
	})["aegis.tool.args_hash"], "a hash computed by the shim wins")
}

func TestLargeUnmappedValuesAreDroppedOnGenAISpans(t *testing.T) {
	load := func() ptrace.Traces {
		td := fixtureByID(t, "openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic").Load(t)
		attrs := firstSpan(td).Attributes()
		attrs.PutStr("x.blob", strings.Repeat("a", 257))
		attrs.PutStr("x.small", strings.Repeat("a", 256))
		list := attrs.PutEmptySlice("x.list")
		for i := 0; i < 3; i++ {
			list.AppendEmpty().SetStr(strings.Repeat("l", 100))
		}
		return td
	}

	h := newHarness(t, nil)
	got := firstSpan(h.run(load())).Attributes().AsRaw()
	assert.NotContains(t, got, "x.blob")
	assert.NotContains(t, got, "x.list")
	assert.Contains(t, got, "x.small")
	assert.Equal(t, map[string]int64{"openllmetry|x.blob": 1, "openllmetry|x.list": 1}, h.counts(droppedLargeMetric, "sdk", "attr"))
	assert.Equal(t, map[string]int64{"openllmetry|x.small": 1}, h.counts(unmappedMetric, "sdk", "attr"),
		"a dropped value is not counted as passed through")
	assertLogsLack(t, h.logs, strings.Repeat("a", 50))

	h = newHarness(t, func(c *Config) { c.MaxUnmappedValueBytes = 1000 })
	assert.Contains(t, firstSpan(h.run(load())).Attributes().AsRaw(), "x.blob", "the limit is config")
}

// FuzzContentAttributesNeverSurvive puts arbitrary values into content attributes; none may
// reach the export. `go test` runs the seed corpus.
func FuzzContentAttributesNeverSurvive(f *testing.F) {
	f.Add(0, "Name the capital of France in one word.")
	f.Add(4, `{"city":"Paris"}`)
	f.Add(18, `[{"role":"user","parts":[{"content":"hello there","type":"text"}]}]`)
	f.Add(21, "{\"units\": \"metric\"}")
	f.Add(-3, "some tool result with\nnewlines\tand tabs")
	f.Fuzz(func(t *testing.T, idx int, value string) {
		fx := fixtureByID(t, "openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic")
		baseline, err := (&ptrace.ProtoMarshaler{}).MarshalTraces(newHarness(t, nil).run(fx.Load(t)))
		require.NoError(t, err)
		if len(value) < 8 || strings.Contains(string(baseline), value) {
			return // too short to be told apart from the rest of the span
		}
		if idx < 0 {
			idx = -idx
		}
		key := contentKeys[idx%len(contentKeys)]
		td := fx.Load(t)
		firstSpan(td).Attributes().PutStr(key, value)
		out, err := (&ptrace.ProtoMarshaler{}).MarshalTraces(newHarness(t, nil).run(td))
		require.NoError(t, err)
		assert.NotContains(t, string(out), value, key)
	})
}
