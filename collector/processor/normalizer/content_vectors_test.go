package normalizer

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.yaml.in/yaml/v3"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/jcs"
)

// The golden vectors of aegis.tool.args_hash (schemas/profile/vectors/README.md). Every
// implementation of the algorithm runs all of them: the Python and TypeScript shims (#12,
// #13) add their own runners; this is the normalizer's. A vector may be added, never
// changed or removed against the base revision (the compat checker enforces that).

type argsHashVector struct {
	ID        string  `json:"id"`
	Input     string  `json:"input"`
	Canonical *string `json:"canonical"`
	Expected  string  `json:"expected"`
	Note      string  `json:"note"`
}

type argsHashVectors struct {
	Attribute        string           `json:"attribute"`
	AlgorithmVersion int              `json:"algorithm_version"`
	Vectors          []argsHashVector `json:"vectors"`
}

func loadArgsHashVectors(t *testing.T) argsHashVectors {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	require.True(t, ok, "cannot locate the source file")
	// collector/processor/normalizer -> the repository root
	path := filepath.Join(filepath.Dir(file), "..", "..", "..", "schemas", "profile", "vectors", "v1", argsHashAttr+".json")
	raw, err := os.ReadFile(path) //nolint:gosec // a fixed path inside the repository
	require.NoError(t, err)
	var doc argsHashVectors
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	require.NoError(t, dec.Decode(&doc))
	return doc
}

func TestArgsHashGoldenVectors(t *testing.T) {
	doc := loadArgsHashVectors(t)
	require.Equal(t, argsHashAttr, doc.Attribute)
	require.NotEmpty(t, doc.Vectors, "no golden vectors registered")
	seen := map[string]bool{}
	for _, v := range doc.Vectors {
		require.False(t, seen[v.ID], "duplicate vector id %s", v.ID)
		seen[v.ID] = true
		t.Run(v.ID, func(t *testing.T) {
			canonical, err := jcs.Canonicalize([]byte(v.Input))
			if v.Canonical == nil {
				assert.Error(t, err, "not I-JSON: the raw UTF-8 bytes are hashed")
			} else {
				require.NoError(t, err)
				assert.Equal(t, *v.Canonical, string(canonical), "RFC 8785 form")
			}
			assert.Equal(t, v.Expected, argsHash(v.Input))
		})
	}
}

func TestArgsHashGoldenVectorsThroughTheProcessor(t *testing.T) {
	for _, v := range loadArgsHashVectors(t).Vectors {
		t.Run(v.ID, func(t *testing.T) {
			td := fixtureByID(t, "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental").Load(t)
			attrs := firstSpan(td).Attributes()
			attrs.PutStr("gen_ai.operation.name", "execute_tool")
			attrs.PutStr("gen_ai.tool.name", "get_weather")
			attrs.PutStr(toolArgsAttr, v.Input)
			got := firstSpan(newHarness(t, nil).run(td)).Attributes().AsRaw()
			assert.Equal(t, v.Expected, got[argsHashAttr])
			assert.NotContains(t, got, toolArgsAttr)
		})
	}
}

// The vectors pin one algorithm_version; the normalizer must implement the one its embedded
// profile declares.
func TestGoldenVectorsMatchTheEmbeddedProfilesAlgorithmVersion(t *testing.T) {
	raw, err := embedded.ReadFile("profiles/aegis-telemetry.v1.yaml")
	require.NoError(t, err)
	var doc struct {
		Attributes []struct {
			Name   string `yaml:"name"`
			Source struct {
				AlgorithmVersion int `yaml:"algorithm_version"`
			} `yaml:"source"`
		} `yaml:"attributes"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))
	declared := 0 // absent
	for _, a := range doc.Attributes {
		if a.Name == argsHashAttr {
			declared = a.Source.AlgorithmVersion
		}
	}
	assert.Equal(t, declared, loadArgsHashVectors(t).AlgorithmVersion)
	assert.Equal(t, 1, declared, "%s: the normalizer implements algorithm_version 1; a new version needs a new implementation", argsHashAttr)
}
