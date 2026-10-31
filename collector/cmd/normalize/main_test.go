package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

func TestRunNormalizesAnExport(t *testing.T) {
	fx := fixtures.All(t)[0]
	raw, err := os.ReadFile(fx.Path)
	require.NoError(t, err)
	cfg := filepath.Join(t.TempDir(), "normalizer.yaml")
	require.NoError(t, os.WriteFile(cfg, []byte("tenant_id: contract-test\n"), 0o600))

	var out bytes.Buffer
	require.NoError(t, run([]string{"--config", cfg}, bytes.NewReader(raw), &out))
	td, err := (&ptrace.JSONUnmarshaler{}).UnmarshalTraces(out.Bytes())
	require.NoError(t, err)
	require.Equal(t, 1, td.SpanCount())
	rs := td.ResourceSpans().At(0)
	tenant, _ := rs.Resource().Attributes().Get("aegis.tenant.id")
	assert.Equal(t, "contract-test", tenant.Str())
	kind, _ := rs.ScopeSpans().At(0).Spans().At(0).Attributes().Get("aegis.span.kind")
	assert.NotEmpty(t, kind.Str())
}

func TestRunWithoutConfigUsesTheDefaults(t *testing.T) {
	raw, err := os.ReadFile(fixtures.All(t)[0].Path)
	require.NoError(t, err)
	var out bytes.Buffer
	require.NoError(t, run(nil, bytes.NewReader(raw), &out))
	assert.Contains(t, out.String(), "aegis-telemetry/1")
}

func TestRunRejectsBadInput(t *testing.T) {
	var out bytes.Buffer
	assert.ErrorContains(t, run(nil, bytes.NewReader([]byte("not json")), &out), "OTLP/JSON")

	cfg := filepath.Join(t.TempDir(), "bad.yaml")
	require.NoError(t, os.WriteFile(cfg, []byte("profile: aegis-telemetry/9\n"), 0o600))
	assert.ErrorContains(t, run([]string{"--config", cfg}, bytes.NewReader([]byte("{}")), &out), "aegis-telemetry/9")

	require.NoError(t, os.WriteFile(cfg, []byte("colour: red\n"), 0o600))
	assert.ErrorContains(t, run([]string{"--config", cfg}, bytes.NewReader([]byte("{}")), &out), "colour")
}
