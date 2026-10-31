package mapping

import (
	"os"
	"path/filepath"
	"testing"
	"testing/fstest"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/profile"
)

func testProfile(t *testing.T) *profile.Profile {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "profiles", "aegis-telemetry.v1.yaml"))
	require.NoError(t, err)
	p, err := profile.Parse(raw)
	require.NoError(t, err)
	return p
}

func embeddedTables(t *testing.T) *Registry {
	t.Helper()
	r, err := Load(os.DirFS(filepath.Join("..", "..", "mappings")), testProfile(t))
	require.NoError(t, err)
	return r
}

func TestTheEmbeddedTablesLoad(t *testing.T) {
	r := embeddedTables(t)
	var ids []string
	for _, tb := range r.Tables() {
		ids = append(ids, tb.ID())
	}
	assert.Equal(t, []string{"openinference/0", "openllmetry/0", "otel-genai/1.30.0", "otel-genai/1.37.0"}, ids)
}

const validTable = `
sdk: openllmetry
version: "0"
source: test
match: {scopes: [s]}
`

func TestLoaderRejectsInvalidTables(t *testing.T) {
	for _, tc := range []struct {
		name  string
		files map[string]string
		err   string
	}{
		{"unknown field", map[string]string{"a/0.yaml": validTable + "colour: red\n"}, "field colour not found"},
		{"sdk outside the profile enum", map[string]string{"a/0.yaml": `
sdk: langsmith
version: "0"
source: test
match: {scopes: [s]}
`}, "sdk langsmith"},
		{"no match", map[string]string{"a/0.yaml": `
sdk: openllmetry
version: "0"
source: test
`}, "match"},
		{"target outside the profile", map[string]string{"a/0.yaml": validTable + `
map:
  - to: gen_ai.request.temperature
    from: [{attr: x}]
`}, "gen_ai.request.temperature is not a span attribute of aegis-telemetry/1"},
		{"target owned by the normalizer", map[string]string{"a/0.yaml": validTable + `
map:
  - to: aegis.span.kind
    from: [{attr: x}]
`}, "aegis.span.kind is set by the normalizer"},
		{"resource target", map[string]string{"a/0.yaml": validTable + `
map:
  - to: aegis.tenant.id
    from: [{attr: x}]
`}, "aegis.tenant.id is not a span attribute"},
		{"source of two kinds", map[string]string{"a/0.yaml": validTable + `
map:
  - to: gen_ai.request.model
    from: [{attr: x, json: y, field: z}]
`}, "exactly one of attr, json, event"},
		{"json without field", map[string]string{"a/0.yaml": validTable + `
map:
  - to: gen_ai.request.model
    from: [{json: y}]
`}, "needs a field"},
		{"unknown convert", map[string]string{"a/0.yaml": validTable + `
map:
  - to: server.address
    from: [{attr: x, convert: shout}]
`}, "convert shout"},
		{"kind outside the profile", map[string]string{"a/0.yaml": validTable + `
kind:
  from: k
  values: {X: {kind: reranker}}
  default: {kind: chain}
`}, "kind reranker"},
		{"operation not allowed for the kind", map[string]string{"a/0.yaml": validTable + `
kind:
  from: k
  values: {X: {kind: chain, operation: chat}}
  default: {kind: chain}
`}, "operation chat is not allowed for kind chain"},
		{"bad known pattern", map[string]string{"a/0.yaml": validTable + `
known: [{pattern: "("}]
`}, "pattern"},
		{"known profile attribute", map[string]string{"a/0.yaml": validTable + `
known: [gen_ai.request.model]
`}, "gen_ai.request.model is a profile attribute"},
		{"two tables without schema_version", map[string]string{
			"a/0.yaml": validTable,
			"a/1.yaml": `
sdk: openllmetry
version: "1"
source: test
match: {scopes: [s]}
`,
		}, "schema_version"},
		{"bad schema_version", map[string]string{"a/0.yaml": validTable + "schema_version: one\n"}, "schema_version one"},
		{"bad content pattern", map[string]string{"a/0.yaml": validTable + `
content: [{pattern: "("}]
`}, "content pattern"},
		{"known and content at once", map[string]string{"a/0.yaml": validTable + `
known: [input.value]
content: [input.value]
`}, "input.value is both known and content"},
		{"content as an attr source", map[string]string{"a/0.yaml": validTable + `
map:
  - to: gen_ai.request.model
    from: [{attr: input.value}]
content: [input.value]
`}, "input.value is content"},
		{"forbidden attribute as an attr source", map[string]string{"a/0.yaml": validTable + `
map:
  - to: gen_ai.request.model
    from: [{attr: gen_ai.input.messages}]
`}, "gen_ai.input.messages is content"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fsys := fstest.MapFS{}
			for name, body := range tc.files {
				fsys[name] = &fstest.MapFile{Data: []byte(body)}
			}
			_, err := Load(fsys, testProfile(t))
			assert.ErrorContains(t, err, tc.err)
		})
	}
}

func TestLoaderNeedsAtLeastOneTable(t *testing.T) {
	_, err := Load(fstest.MapFS{}, testProfile(t))
	assert.ErrorContains(t, err, "no mapping tables")
}
