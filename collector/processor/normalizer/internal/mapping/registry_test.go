package mapping

import (
	"testing"

	"github.com/stretchr/testify/assert"
	"go.opentelemetry.io/collector/pdata/pcommon"
)

func TestSelect(t *testing.T) {
	r := embeddedTables(t)
	none := pcommon.NewMap()
	marked := pcommon.NewMap()
	marked.PutStr("openinference.span.kind", "LLM")

	for _, tc := range []struct {
		name      string
		scope     Scope
		attrs     pcommon.Map
		table     string // "" for no table
		newer     bool
		schemaURL string
	}{
		{"scope prefix", Scope{Name: "openinference.instrumentation.langchain"}, none, "openinference/0", false, ""},
		{"exact scope", Scope{Name: "opentelemetry.instrumentation.openai.v1"}, none, "openllmetry/0", false, ""},
		{"exact scope only", Scope{Name: "opentelemetry.instrumentation.openai.v10"}, none, "", false, ""},
		{"marker attribute", Scope{Name: "my.app"}, marked, "openinference/0", false, ""},
		{"no table", Scope{Name: "io.opentelemetry.http"}, none, "", false, ""},
		{"schema 1.30.0", Scope{Name: "opentelemetry.instrumentation.openai_v2", SchemaURL: "https://opentelemetry.io/schemas/1.30.0"}, none, "otel-genai/1.30.0", false, ""},
		{"schema between tables", Scope{Name: "opentelemetry.instrumentation.openai_v2", SchemaURL: "https://opentelemetry.io/schemas/1.33.0"}, none, "otel-genai/1.30.0", false, ""},
		{"schema 1.37.0", Scope{Name: "opentelemetry.util.genai.handler", SchemaURL: "https://opentelemetry.io/schemas/1.37.0"}, none, "otel-genai/1.37.0", false, ""},
		{"older than every table", Scope{Name: "opentelemetry.instrumentation.openai_v2", SchemaURL: "https://opentelemetry.io/schemas/1.26.0"}, none, "otel-genai/1.30.0", false, ""},
		{"newer core schema", Scope{Name: "opentelemetry.util.genai.handler", SchemaURL: "https://opentelemetry.io/schemas/1.44.0"}, none, "otel-genai/1.37.0", true, "https://opentelemetry.io/schemas/1.44.0"},
		{"GenAI dev schema", Scope{Name: "opentelemetry.util.genai.handler", SchemaURL: "https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev"}, none, "otel-genai/1.37.0", true, "https://opentelemetry.io/schemas/gen-ai-dev/1.42.0-dev"},
		{"resource schema_url", Scope{Name: "opentelemetry.util.genai.handler", ResourceSchemaURL: "https://opentelemetry.io/schemas/1.30.0"}, none, "otel-genai/1.30.0", false, ""},
		{"no schema_url: newest", Scope{Name: "opentelemetry.util.genai.handler"}, none, "otel-genai/1.37.0", false, ""},
		{"unparseable schema_url: newest", Scope{Name: "opentelemetry.util.genai.handler", SchemaURL: "https://example.com/my-schema"}, none, "otel-genai/1.37.0", false, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sel := r.Select(tc.scope, tc.attrs)
			if tc.table == "" {
				assert.Nil(t, sel.Table)
				assert.Equal(t, "unknown", sel.SDK())
				return
			}
			if assert.NotNil(t, sel.Table) {
				assert.Equal(t, tc.table, sel.Table.ID())
			}
			assert.Equal(t, tc.newer, sel.Newer)
			assert.Equal(t, tc.schemaURL, sel.NewerSchemaURL)
		})
	}
}

func TestParseSchemaURL(t *testing.T) {
	for _, tc := range []struct {
		url string
		ok  bool
		cmp string // compared with 1.37.0: "<", "=", ">"
	}{
		{"https://opentelemetry.io/schemas/1.37.0", true, "="},
		{"https://opentelemetry.io/schemas/1.30.0", true, "<"},
		{"https://opentelemetry.io/schemas/1.44.0", true, ">"},
		{"https://opentelemetry.io/schemas/1.4.0", true, "<"},
		{"https://opentelemetry.io/schemas/gen-ai-dev/1.0.0-dev", true, ">"},
		{"https://opentelemetry.io/schemas/1.37", false, ""},
		{"https://example.com/schemas/1.37.0", false, ""},
		{"", false, ""},
	} {
		t.Run(tc.url, func(t *testing.T) {
			v, ok := parseSchemaURL(tc.url)
			assert.Equal(t, tc.ok, ok)
			if !ok {
				return
			}
			ref, _ := parseSchemaURL("https://opentelemetry.io/schemas/1.37.0")
			got := map[int]string{-1: "<", 0: "=", 1: ">"}[v.compare(ref)]
			assert.Equal(t, tc.cmp, got)
		})
	}
}
