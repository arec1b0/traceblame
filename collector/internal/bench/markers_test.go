package bench

import (
	"context"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/client"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// Eval and fault-injection traces (#36) through the kind pipeline as replayed here: agent
// normalizer (authenticated tenant, allowlist), gateway normalizer (same allowlist), then
// the gateway's tail_sampling policies.

func agentConfig() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", "..", "..", "deploy", "kind", "collector", "agent.yaml")
}

var allowlist = map[string]string{"TRACEBLAME_EVAL_TENANTS": "[tenant-eval]"}

type basicAuth string

func (u basicAuth) GetAttribute(name string) any {
	if name == "username" {
		return string(u)
	}
	return nil
}

func (basicAuth) GetAttributeNames() []string { return []string{"username"} }

// throughBothTiers normalizes td as the agent does for tenant, then as the gateway does.
func throughBothTiers(t *testing.T, td ptrace.Traces, tenant string) ptrace.Traces {
	t.Helper()
	agent, err := NewNormalizer(agentConfig(), allowlist)
	require.NoError(t, err)
	gateway, err := NewNormalizer(gatewayConfig(), allowlist)
	require.NoError(t, err)
	ctx := client.NewContext(context.Background(), client.Info{Auth: basicAuth(tenant)})
	td, err = agent.Run(ctx, td)
	require.NoError(t, err)
	td, err = gateway.Run(context.Background(), td)
	require.NoError(t, err)
	return td
}

func markRun(td ptrace.Traces, id string) ptrace.Traces {
	for i := 0; i < td.ResourceSpans().Len(); i++ {
		td.ResourceSpans().At(i).Resource().Attributes().PutStr("aegis.eval.run.id", id)
	}
	return td
}

// markInjection marks one child span only: faults/ marks the span it injects at. Spans
// are exported as they end and the root ends last, so the first export is a child.
func markInjection(td ptrace.Traces, id string) ptrace.Traces {
	td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).
		Attributes().PutStr("aegis.fault.injection.id", id)
	return td
}

// fastOK returns n traces that no policy but the markers would keep at 100%: no error, and
// faster than every latency threshold.
func fastOK(t *testing.T, g *Generator, n int) []Trace {
	t.Helper()
	var out []Trace
	for seq := uint32(0); len(out) < n; seq++ {
		require.Less(t, seq, uint32(10_000), "the generator yields fast ok traces")
		// Faster than LATENCY_P95_MS_* of the replay (thresholds, 5000 ms).
		if tr := g.Trace(seq); !tr.Error && tr.Duration < (5*time.Second).Nanoseconds() {
			out = append(out, tr)
		}
	}
	return out
}

func TestMarkedTracesOfAnEvalTenantAreKeptAtOneHundredPercent(t *testing.T) {
	g := generator(t, params)
	traces := fastOK(t, g, 160)
	var parts []ptrace.Traces
	for i, tr := range traces[:80] {
		td := whole(g, tr)
		if i%2 == 0 {
			td = markRun(td, "run-bench-1")
		} else {
			td = markInjection(td, "inj-bench-1")
		}
		parts = append(parts, throughBothTiers(t, td, "tenant-eval"))
	}
	// The same marking from a tenant that is not allowlisted.
	other := traces[80:]
	for i, tr := range other {
		td := whole(g, tr)
		if i%2 == 0 {
			td = markRun(td, "run-bench-1")
		} else {
			td = markInjection(td, "inj-bench-1")
		}
		parts = append(parts, throughBothTiers(t, td, "tenant-a"))
	}
	sampled, err := sampler(t).Decide(parts)
	require.NoError(t, err)

	for _, tr := range traces[:80] {
		assert.True(t, sampled[tr.ID], "an eval tenant's marked fast ok trace is kept")
	}

	// Unmarked, the same trace ids get the same decisions: the markers were dropped, so
	// the tenant's traces are sampled like any other (the baseline hashes the trace id).
	var unmarked []ptrace.Traces
	for _, tr := range other {
		unmarked = append(unmarked, throughBothTiers(t, whole(g, tr), "tenant-a"))
	}
	baseline, err := sampler(t).Decide(unmarked)
	require.NoError(t, err)
	kept := 0
	for _, tr := range other {
		assert.Equal(t, baseline[tr.ID], sampled[tr.ID], "a non-allowlisted marker changes nothing")
		if sampled[tr.ID] {
			kept++
		}
	}
	assert.Less(t, kept, len(other)/2, "and most of them are dropped (10%% baseline)")
}
