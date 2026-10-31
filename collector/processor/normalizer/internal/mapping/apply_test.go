package mapping

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

func table(t *testing.T, id string) *Table {
	t.Helper()
	for _, tb := range embeddedTables(t).Tables() {
		if tb.ID() == id {
			return tb
		}
	}
	t.Fatalf("no table %s", id)
	return nil
}

func span(attrs map[string]any) ptrace.Span {
	s := ptrace.NewSpan()
	if err := s.Attributes().FromRaw(attrs); err != nil {
		panic(err)
	}
	return s
}

func TestApplyKeepsTheNativeProfileNameAndConsumesTheSource(t *testing.T) {
	s := span(map[string]any{"gen_ai.provider.name": "anthropic", "gen_ai.system": "openai"})
	table(t, "otel-genai/1.30.0").Apply(s)
	assert.Equal(t, map[string]any{"gen_ai.provider.name": "anthropic"}, s.Attributes().AsRaw())
}

func TestApplyRenames(t *testing.T) {
	s := span(map[string]any{"gen_ai.system": "openai", "server.port": int64(11434)})
	table(t, "otel-genai/1.30.0").Apply(s)
	assert.Equal(t, map[string]any{"gen_ai.provider.name": "openai", "server.port": int64(11434)},
		s.Attributes().AsRaw())
}

func TestApplyMapsValuesInPlace(t *testing.T) {
	s := span(map[string]any{"gen_ai.response.finish_reasons": []any{"tool_call", "stop"}})
	table(t, "openllmetry/0").Apply(s)
	assert.Equal(t, []any{"tool_calls", "stop"}, s.Attributes().AsRaw()["gen_ai.response.finish_reasons"])
}

func TestApplyDerivesTheServerAddressAndKeepsTheSource(t *testing.T) {
	s := span(map[string]any{"gen_ai.openai.api_base": "http://localhost:11434/v1/"})
	table(t, "openllmetry/0").Apply(s)
	assert.Equal(t, map[string]any{
		"gen_ai.openai.api_base": "http://localhost:11434/v1/",
		"server.address":         "localhost",
	}, s.Attributes().AsRaw())
}

func TestApplyFallsBackAcrossSources(t *testing.T) {
	tb := table(t, "openinference/0")

	s := span(map[string]any{
		"llm.invocation_parameters": `{"model": "requested", "max_tokens": 16}`,
		"llm.model_name":            "served",
	})
	tb.Apply(s)
	got := s.Attributes().AsRaw()
	assert.Equal(t, "requested", got["gen_ai.request.model"])
	assert.Equal(t, "served", got["gen_ai.response.model"])
	assert.Equal(t, int64(16), got["gen_ai.request.max_tokens"])
	assert.NotContains(t, got, "llm.model_name")
	assert.Contains(t, got, "llm.invocation_parameters", "json sources are not consumed")

	s = span(map[string]any{"llm.invocation_parameters": "not json", "llm.model_name": "served"})
	tb.Apply(s)
	assert.Equal(t, "served", s.Attributes().AsRaw()["gen_ai.request.model"])
}

func TestApplyReadsExceptionEventsOnlyOnErrors(t *testing.T) {
	tb := table(t, "openinference/0")
	withException := func(code ptrace.StatusCode) ptrace.Span {
		s := span(map[string]any{})
		s.Status().SetCode(code)
		ev := s.Events().AppendEmpty()
		ev.SetName("exception")
		ev.Attributes().PutStr("exception.type", "openai.BadRequestError")
		return s
	}

	s := withException(ptrace.StatusCodeError)
	tb.Apply(s)
	v, ok := s.Attributes().Get("error.type")
	require.True(t, ok)
	assert.Equal(t, "openai.BadRequestError", v.Str())

	s = withException(ptrace.StatusCodeOk)
	tb.Apply(s)
	_, ok = s.Attributes().Get("error.type")
	assert.False(t, ok)
}

func TestKind(t *testing.T) {
	tb := table(t, "openinference/0")
	for _, tc := range []struct {
		name, spanName, value string
		kind, op              string
	}{
		{"chat", "ChatCompletion", "LLM", "llm", "chat"},
		{"completion by span name", "Completion", "LLM", "llm", "text_completion"},
		{"embedding", "CreateEmbeddings", "EMBEDDING", "embedding", "embeddings"},
		{"retriever", "retrieve", "RETRIEVER", "retrieval", "retrieval"},
		{"chain has no operation", "chain", "CHAIN", "chain", ""},
		{"reranker falls back to chain", "rerank", "RERANKER", "chain", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := span(map[string]any{"openinference.span.kind": tc.value})
			s.SetName(tc.spanName)
			kind, op, ok := tb.Kind(s)
			assert.True(t, ok)
			assert.Equal(t, tc.kind, kind)
			assert.Equal(t, tc.op, op)
		})
	}

	_, _, ok := tb.Kind(span(map[string]any{}))
	assert.False(t, ok, "no kind attribute")
	_, _, ok = table(t, "otel-genai/1.37.0").Kind(span(map[string]any{"gen_ai.operation.name": "chat"}))
	assert.False(t, ok, "tables without a kind rule leave the kind to gen_ai.operation.name")
}

func TestKnown(t *testing.T) {
	tb := table(t, "openinference/0")
	assert.True(t, tb.Known("llm.token_count.total"))
	assert.True(t, tb.Known("embedding.embeddings.3.embedding.vector"))
	assert.False(t, tb.Known("embedding.embeddings.x.embedding.vector"))
	assert.False(t, tb.Known("llm.tools.0.tool.json_schema"))
}

func TestApplyIsDeterministic(t *testing.T) {
	tb := table(t, "openinference/0")
	var first []string
	for i := 0; i < 20; i++ {
		s := span(map[string]any{
			"llm.system":                 "openai",
			"llm.model_name":             "m",
			"llm.token_count.prompt":     int64(1),
			"llm.token_count.completion": int64(2),
			"llm.finish_reason":          "stop",
			"openinference.span.kind":    "LLM",
		})
		tb.Apply(s)
		var keys []string
		s.Attributes().Range(func(k string, _ pcommon.Value) bool {
			keys = append(keys, k)
			return true
		})
		if first == nil {
			first = keys
			continue
		}
		assert.Equal(t, first, keys)
	}
}
