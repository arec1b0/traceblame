package profile

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/pcommon"
)

func loadV1(t *testing.T) *Profile {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "profiles", "aegis-telemetry.v1.yaml"))
	require.NoError(t, err)
	p, err := Parse(raw)
	require.NoError(t, err)
	return p
}

func TestParseTheEmbeddedProfile(t *testing.T) {
	p := loadV1(t)
	assert.Equal(t, "aegis-telemetry/1", p.ID)
	assert.Regexp(t, `^\d+\.\d+\.\d+$`, p.Revision)

	kind, ok := p.KindForOperation("chat")
	assert.True(t, ok)
	assert.Equal(t, "llm", kind)
	kind, ok = p.KindForOperation("embeddings")
	assert.True(t, ok)
	assert.Equal(t, "embedding", kind)
	_, ok = p.KindForOperation("no_such_operation")
	assert.False(t, ok)
	assert.True(t, p.OperationAllowed("tool", "execute_tool"))
	assert.False(t, p.OperationAllowed("chain", "chat"))
	assert.True(t, p.HasKind("retrieval"))
	assert.False(t, p.HasKind("reranker"))

	a, ok := p.Attribute("gen_ai.request.max_tokens")
	require.True(t, ok)
	assert.Equal(t, "span", a.Scope)
	assert.Equal(t, "int", a.Type)
	a, ok = p.Attribute("aegis.tenant.id")
	require.True(t, ok)
	assert.Equal(t, "resource", a.Scope)
	sdk, ok := p.Attribute("aegis.source.sdk")
	require.True(t, ok)
	assert.Contains(t, sdk.Enum, "openinference")

	assert.True(t, p.IsForbidden("gen_ai.input.messages"))
	assert.False(t, p.IsForbidden("gen_ai.request.model"))
}

func TestParseRejectsAProfileWithoutID(t *testing.T) {
	_, err := Parse([]byte("major: 1\n"))
	assert.ErrorContains(t, err, "profile")
}

func strs(vs ...string) pcommon.Value {
	v := pcommon.NewValueSlice()
	for _, s := range vs {
		v.Slice().AppendEmpty().SetStr(s)
	}
	return v
}

func TestCoerce(t *testing.T) {
	p := loadV1(t)
	attr := func(name string) *Attribute {
		a, ok := p.Attribute(name)
		require.True(t, ok, name)
		return a
	}
	for _, tc := range []struct {
		name string
		attr string
		in   pcommon.Value
		want any // nil: rejected
	}{
		{"int stays", "gen_ai.request.max_tokens", pcommon.NewValueInt(16), int64(16)},
		{"decimal string to int", "gen_ai.request.max_tokens", pcommon.NewValueStr("16"), int64(16)},
		{"integral double to int", "gen_ai.usage.input_tokens", pcommon.NewValueDouble(38), int64(38)},
		{"fractional double is not an int", "gen_ai.usage.input_tokens", pcommon.NewValueDouble(3.5), nil},
		{"word is not an int", "gen_ai.request.max_tokens", pcommon.NewValueStr("sixteen"), nil},
		{"string stays", "gen_ai.request.model", pcommon.NewValueStr("qwen2.5:7b"), "qwen2.5:7b"},
		{"int to string", "aegis.session.id", pcommon.NewValueInt(42), "42"},
		{"bool is not a string", "gen_ai.request.model", pcommon.NewValueBool(true), nil},
		{"scalar to string array", "gen_ai.response.finish_reasons", pcommon.NewValueStr("stop"), []any{"stop"}},
		{"string array stays", "gen_ai.response.finish_reasons", strs("stop", "length"), []any{"stop", "length"}},
		{"double array from ints", "aegis.retrieval.scores", func() pcommon.Value {
			v := pcommon.NewValueSlice()
			v.Slice().AppendEmpty().SetInt(1)
			v.Slice().AppendEmpty().SetDouble(0.5)
			return v
		}(), []any{1.0, 0.5}},
		{"pattern holds", "aegis.bundle.digest", pcommon.NewValueStr("sha256:" + sixtyFourHex), "sha256:" + sixtyFourHex},
		{"pattern fails", "aegis.bundle.digest", pcommon.NewValueStr("sha256:short"), nil},
		{"enum holds", "aegis.span.kind", pcommon.NewValueStr("tool"), "tool"},
		{"enum fails", "aegis.span.kind", pcommon.NewValueStr("reranker"), nil},
		{"const fails", "aegis.profile.version", pcommon.NewValueStr("aegis-telemetry/0"), nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := attr(tc.attr).Coerce(tc.in)
			if tc.want == nil {
				assert.False(t, ok)
				return
			}
			require.True(t, ok)
			assert.Equal(t, tc.want, got.AsRaw())
		})
	}
}

const sixtyFourHex = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
