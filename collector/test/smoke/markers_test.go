package smoke

import (
	"bytes"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.yaml.in/yaml/v3"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

// Eval and fault-injection markers (#36). Both tiers run the normalizer with one allowlist,
// from one source: deploy/kind/collector/eval-tenants.env -> ConfigMap eval-tenants ->
// TRACEBLAME_EVAL_TENANTS in both pods -> eval_tenants in both collector configs.

const evalTenantsEnv = "TRACEBLAME_EVAL_TENANTS"

const markerAgentConfig = `
extensions:
  health_check:
    endpoint: 127.0.0.1:{{.Health}}
  basicauth/apps:
    htpasswd:
      inline: |
        tenant-a:{SHA}__HASH__
        tenant-b:{SHA}__HASH__
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{{.OTLP}}
        auth:
          authenticator: basicauth/apps
processors:
  normalizer:
    tenant_source: auth
    eval_tenants: ${env:TRACEBLAME_EVAL_TENANTS}
exporters:
  file:
    path: {{.Out}}
    format: json
service:
  extensions: [health_check, basicauth/apps]
  telemetry:
    metrics: {level: none}
    logs: {output_paths: [{{.Log}}], error_output_paths: [{{.Log}}]}
  pipelines:
    traces:
      receivers: [otlp]
      processors: [normalizer]
      exporters: [file]
`

// The allowlist arrives as an environment variable holding a YAML list, as in kind; the
// collector's confmap must read it as a list, not as the string "[tenant-a]".
func TestTheAgentKeepsMarkersOnlyForTenantsInTheEnvAllowlist(t *testing.T) {
	t.Setenv(evalTenantsEnv, "[tenant-a]")
	cfg := strings.ReplaceAll(markerAgentConfig, "__HASH__", shaPassword("secret"))
	c, _ := startCustom(t, cfg)

	td := fixtures.All(t)[0].Load(t)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("aegis.eval.run.id", "run-smoke-1")
	td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().PutStr("aegis.fault.injection.id", "inj-smoke-1")
	body, err := (&ptrace.JSONMarshaler{}).MarshalTraces(td)
	require.NoError(t, err)
	for _, user := range []string{"tenant-a", "tenant-b"} {
		req, err := http.NewRequest(http.MethodPost, c.otlpURL, bytes.NewReader(body)) //nolint:noctx // local test
		require.NoError(t, err)
		req.Header.Set("Content-Type", "application/json")
		req.SetBasicAuth(user, "secret")
		resp, err := http.DefaultClient.Do(req)
		require.NoError(t, err)
		_ = resp.Body.Close()
		require.Equal(t, http.StatusOK, resp.StatusCode)
	}

	exported := c.exported(t)
	require.Len(t, exported, 2)
	got := map[string][2]bool{}
	for _, e := range exported {
		rs := e.ResourceSpans().At(0)
		tenant, _ := rs.Resource().Attributes().Get("aegis.tenant.id")
		_, run := rs.Resource().Attributes().Get("aegis.eval.run.id")
		_, inj := rs.ScopeSpans().At(0).Spans().At(0).Attributes().Get("aegis.fault.injection.id")
		got[tenant.Str()] = [2]bool{run, inj}
	}
	assert.Equal(t, map[string][2]bool{"tenant-a": {true, true}, "tenant-b": {false, false}}, got)
}

type manifestDoc struct {
	Kind     string `yaml:"kind"`
	Metadata struct {
		Name string `yaml:"name"`
	} `yaml:"metadata"`
	Spec struct {
		Template struct {
			Spec struct {
				Containers []struct {
					Name    string `yaml:"name"`
					EnvFrom []struct {
						ConfigMapRef struct {
							Name string `yaml:"name"`
						} `yaml:"configMapRef"`
					} `yaml:"envFrom"`
				} `yaml:"containers"`
			} `yaml:"spec"`
		} `yaml:"template"`
	} `yaml:"spec"`
}

func configMapsOf(t *testing.T, manifest, kind, name string) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(repoPath("deploy", "kind", "manifests", manifest)))
	require.NoError(t, err)
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	for {
		var doc manifestDoc
		if err := dec.Decode(&doc); err != nil {
			break
		}
		if doc.Kind != kind || doc.Metadata.Name != name {
			continue
		}
		var out []string
		for _, c := range doc.Spec.Template.Spec.Containers {
			if c.Name != "otelcol" {
				continue
			}
			for _, ref := range c.EnvFrom {
				out = append(out, ref.ConfigMapRef.Name)
			}
		}
		return out
	}
	t.Fatalf("no %s %s in %s", kind, name, manifest)
	return nil
}

func evalTenantsOf(t *testing.T, config string) any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(repoPath("deploy", "kind", "collector", config)))
	require.NoError(t, err)
	var doc struct {
		Processors map[string]map[string]any `yaml:"processors"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &doc))
	return doc.Processors["normalizer"]["eval_tenants"]
}

// The gateway's second pass must keep what the agent accepted, so both read one allowlist.
func TestBothTiersReadTheEvalAllowlistFromOneSource(t *testing.T) {
	ref := "${env:" + evalTenantsEnv + "}"
	assert.Equal(t, ref, evalTenantsOf(t, "agent.yaml"))
	assert.Equal(t, ref, evalTenantsOf(t, "gateway.yaml"))

	assert.Contains(t, configMapsOf(t, "20-agent.yaml", "DaemonSet", "otel-agent"), "eval-tenants")
	assert.Contains(t, configMapsOf(t, "30-gateway.yaml", "Deployment", "otel-gateway"), "eval-tenants")

	lines := readEnv(t, repoPath("deploy", "kind", "collector", "eval-tenants.env"))
	require.Len(t, lines, 1, "one variable, nothing else")
	assert.True(t, strings.HasPrefix(lines[0], evalTenantsEnv+"="), lines[0])

	deploy, err := os.ReadFile(filepath.Clean(repoPath("scripts", "kind-deploy.sh")))
	require.NoError(t, err)
	assert.Contains(t, string(deploy), "create configmap eval-tenants --from-env-file=\"${collector}/eval-tenants.env\"")
	assert.Contains(t, string(deploy), "rollout restart daemonset/otel-agent deployment/otel-gateway",
		"one rollout restarts both tiers")
}
