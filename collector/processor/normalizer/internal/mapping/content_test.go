package mapping

import (
	"testing"

	"github.com/stretchr/testify/assert"
)

func TestContentCoversTheProfileAndEveryTable(t *testing.T) {
	r := embeddedTables(t)
	for _, name := range []string{
		"gen_ai.input.messages",                   // profile forbidden list
		"gen_ai.tool.call.arguments",              // profile forbidden list
		"input.value",                             // openinference
		"llm.output_messages.0.message.content",   // openinference
		"llm.tools.0.tool.json_schema",            // openinference
		"embedding.embeddings.0.embedding.vector", // openinference
		"retrieval.documents.3.document.content",  // openinference
		"gen_ai.prompt.0.content",                 // openllmetry
		"traceloop.entity.output",                 // openllmetry
		"gen_ai.tool.definitions",                 // openllmetry, otel-genai
	} {
		assert.True(t, r.IsContent(name), name)
	}
	for _, name := range []string{
		"gen_ai.request.model",
		"retrieval.documents.3.document.id",
		"retrieval.documents.3.document.score",
		"input.mime_type",
		"llm.token_count.total",
	} {
		assert.False(t, r.IsContent(name), name)
	}
}
