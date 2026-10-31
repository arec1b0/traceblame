package normalizer

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/mapping"
)

// Eval and fault-injection markers (#36): the gateway keeps marked traces at 100%, so only
// tenants in eval_tenants may set them; every other marker is dropped and counted.

const (
	rejectedMetric = "traceblame_normalizer_eval_marker_rejected_total"
	runID          = "run-canary-7f3a"
	injectionID    = "inj-canary-91c2"
)

func allowlisted(c *Config) { c.EvalTenants = []string{testTenant} }

// withMarkers puts aegis.eval.run.id on every resource and aegis.fault.injection.id on
// every span, and returns the number of resources and spans.
func withMarkers(td ptrace.Traces, run, injection pcommon.Value) (resources, spans int) {
	for i := 0; i < td.ResourceSpans().Len(); i++ {
		run.CopyTo(td.ResourceSpans().At(i).Resource().Attributes().PutEmpty(evalRunAttr))
		resources++
	}
	eachSpan(td, func(_ pcommon.Resource, _ ptrace.ScopeSpans, span ptrace.Span) {
		injection.CopyTo(span.Attributes().PutEmpty(faultInjectionAttr))
		spans++
	})
	return resources, spans
}

func marked(t *testing.T, id string) (ptrace.Traces, int, int) {
	t.Helper()
	td := fixtureByID(t, id).Load(t)
	r, s := withMarkers(td, pcommon.NewValueStr(runID), pcommon.NewValueStr(injectionID))
	return td, r, s
}

// markersOf returns every marker value left in td.
func markersOf(td ptrace.Traces) (runs, injections []string) {
	for i := 0; i < td.ResourceSpans().Len(); i++ {
		if v, ok := td.ResourceSpans().At(i).Resource().Attributes().Get(evalRunAttr); ok {
			runs = append(runs, v.AsString())
		}
	}
	eachSpan(td, func(_ pcommon.Resource, _ ptrace.ScopeSpans, span ptrace.Span) {
		if v, ok := span.Attributes().Get(faultInjectionAttr); ok {
			injections = append(injections, v.AsString())
		}
	})
	return runs, injections
}

func repeat(s string, n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = s
	}
	return out
}

// tableOf names the mapping table the first span of td selects ("" when none does).
func tableOf(t *testing.T, td ptrace.Traces) string {
	t.Helper()
	p, err := newProcessor(createDefaultConfig().(*Config), componenttest.NewNopTelemetrySettings())
	require.NoError(t, err)
	rs := td.ResourceSpans().At(0)
	ss := rs.ScopeSpans().At(0)
	sel := p.tables.Select(mapping.Scope{Name: ss.Scope().Name(), SchemaURL: ss.SchemaUrl(), ResourceSchemaURL: rs.SchemaUrl()},
		ss.Spans().At(0).Attributes())
	if sel.Table == nil {
		return ""
	}
	return sel.Table.SDK + "/" + sel.Table.Version
}

func outsideEveryTable(td ptrace.Traces) {
	for i := 0; i < td.ResourceSpans().Len(); i++ {
		rs := td.ResourceSpans().At(i)
		rs.SetSchemaUrl("")
		for j := 0; j < rs.ScopeSpans().Len(); j++ {
			rs.ScopeSpans().At(j).Scope().SetName("my.app.agent")
			rs.ScopeSpans().At(j).SetSchemaUrl("")
		}
	}
	eachSpan(td, func(_ pcommon.Resource, _ ptrace.ScopeSpans, span ptrace.Span) {
		span.Attributes().RemoveIf(func(k string, _ pcommon.Value) bool { return strings.HasPrefix(k, "openinference.") })
	})
}

// Markers are native profile names: no table maps them, and none may lose them. They do
// not depend on SDK detection either.
func TestEvalMarkersAreKeptOnEveryTableAndWithoutOneForAnAllowlistedTenant(t *testing.T) {
	for _, tc := range []struct {
		fixture string
		table   string
		mutate  func(ptrace.Traces)
	}{
		{"openinference/openinference-instrumentation-openai-0.1.68/chat_basic", "openinference/0", nil},
		{"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic", "openllmetry/0", nil},
		{"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic", "otel-genai/1.30.0", nil},
		{"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental", "otel-genai/1.37.0", nil},
		{"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic", "", outsideEveryTable},
	} {
		name := tc.table
		if name == "" {
			name = "no table"
		}
		t.Run(name, func(t *testing.T) {
			td, resources, spans := marked(t, tc.fixture)
			if tc.mutate != nil {
				tc.mutate(td)
			}
			require.Equal(t, tc.table, tableOf(t, td))

			h := newHarness(t, allowlisted)
			runs, injections := markersOf(h.run(td))
			assert.Equal(t, repeat(runID, resources), runs)
			assert.Equal(t, repeat(injectionID, spans), injections)
			assert.Empty(t, h.countsBy(rejectedMetric, "tenant", "attr", "reason"))
			assert.Empty(t, h.counts(invalidMetric, "sdk", "attr"))
		})
	}
}

func TestEvalMarkersAreDroppedAndCountedForATenantNotAllowlisted(t *testing.T) {
	for _, fixture := range []string{
		"openinference/openinference-instrumentation-openai-0.1.68/chat_basic",
		"openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic",
		"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic",
		"otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic_latest_experimental",
	} {
		t.Run(fixture, func(t *testing.T) {
			td, resources, spans := marked(t, fixture)
			h := newHarness(t, func(c *Config) { c.EvalTenants = []string{"eval-runner"} })
			runs, injections := markersOf(h.run(td))
			assert.Empty(t, runs)
			assert.Empty(t, injections)
			assert.Equal(t, map[string]int64{
				testTenant + "|aegis.eval.run.id|tenant_not_allowed":        int64(resources),
				testTenant + "|aegis.fault.injection.id|tenant_not_allowed": int64(spans),
			}, h.countsBy(rejectedMetric, "tenant", "attr", "reason"))
			assert.Empty(t, h.counts(invalidMetric, "sdk", "attr"), "a security signal, not an invalid attribute")
		})
	}
}

func TestTheDefaultConfigAllowlistsNoTenant(t *testing.T) {
	td, _, _ := marked(t, chatFixture)
	h := newHarness(t, nil)
	runs, injections := markersOf(h.run(td))
	assert.Empty(t, runs)
	assert.Empty(t, injections)
	assert.NotEmpty(t, h.countsBy(rejectedMetric, "tenant", "attr", "reason"))
}

func TestEvalMarkersFailingTheProfilePatternAreRejected(t *testing.T) {
	for name, v := range map[string]pcommon.Value{
		"space":       pcommon.NewValueStr("run 1"),
		"129 chars":   pcommon.NewValueStr(strings.Repeat("a", 129)),
		"empty":       pcommon.NewValueStr(""),
		"non-ASCII":   pcommon.NewValueStr("прогон-1"),
		"not a value": pcommon.NewValueBool(true),
	} {
		t.Run(name, func(t *testing.T) {
			td := fixtureByID(t, chatFixture).Load(t)
			resources, spans := withMarkers(td, v, v)
			h := newHarness(t, allowlisted)
			runs, injections := markersOf(h.run(td))
			assert.Empty(t, runs)
			assert.Empty(t, injections)
			assert.Equal(t, map[string]int64{
				testTenant + "|aegis.eval.run.id|pattern":        int64(resources),
				testTenant + "|aegis.fault.injection.id|pattern": int64(spans),
			}, h.countsBy(rejectedMetric, "tenant", "attr", "reason"))
			assert.Empty(t, h.counts(invalidMetric, "sdk", "attr"))
		})
	}
}

func TestAnIntegerMarkerIsRepairedLikeAnyStringAttribute(t *testing.T) {
	td := fixtureByID(t, chatFixture).Load(t)
	withMarkers(td, pcommon.NewValueInt(42), pcommon.NewValueInt(7))
	runs, injections := markersOf(newHarness(t, allowlisted).run(td))
	assert.Equal(t, []string{"42"}, runs)
	assert.Equal(t, []string{"7"}, injections)
}

func TestTheTenantIsCheckedBeforeTheValue(t *testing.T) {
	td := fixtureByID(t, chatFixture).Load(t)
	bad := pcommon.NewValueStr("run 1")
	withMarkers(td, bad, bad)
	h := newHarness(t, nil)
	h.run(td)
	for k := range h.countsBy(rejectedMetric, "tenant", "attr", "reason") {
		assert.True(t, strings.HasSuffix(k, "|tenant_not_allowed"), k)
	}
}

func TestEvalMarkersInTheWrongScopeStayInvalidAttributes(t *testing.T) {
	td := fixtureByID(t, chatFixture).Load(t)
	firstSpan(td).Attributes().PutStr(evalRunAttr, runID)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr(faultInjectionAttr, injectionID)
	h := newHarness(t, allowlisted)
	runs, injections := markersOf(h.run(td))
	assert.Empty(t, runs)
	assert.Empty(t, injections)
	assert.Equal(t, map[string]int64{
		"otel-genai|aegis.eval.run.id":     1,
		"unknown|aegis.fault.injection.id": 1,
	}, h.counts(invalidMetric, "sdk", "attr"))
	assert.Empty(t, h.countsBy(rejectedMetric, "tenant", "attr", "reason"))
}

func TestEvalMarkersFollowTheResolvedTenant(t *testing.T) {
	agent := func(c *Config) {
		c.TenantSource, c.TenantID, c.EvalTenants = TenantSourceAuth, "", []string{"tenant-eval"}
	}
	t.Run("auth: the authenticated tenant, not the shim's", func(t *testing.T) {
		td, _, _ := marked(t, chatFixture)
		td.ResourceSpans().At(0).Resource().Attributes().PutStr(tenantAttr, "tenant-eval")
		h := newHarness(t, agent)
		runs, _ := markersOf(h.runCtx(withAuth("tenant-a"), td))
		assert.Empty(t, runs, "a shim cannot claim an allowlisted tenant")
		assert.Contains(t, h.countsBy(rejectedMetric, "tenant", "attr", "reason"),
			"tenant-a|aegis.eval.run.id|tenant_not_allowed")
	})
	t.Run("auth without authentication", func(t *testing.T) {
		td, _, _ := marked(t, chatFixture)
		h := newHarness(t, agent)
		runs, _ := markersOf(h.runCtx(context.Background(), td))
		assert.Empty(t, runs)
		assert.Contains(t, h.countsBy(rejectedMetric, "tenant", "attr", "reason"),
			"_none|aegis.eval.run.id|tenant_not_allowed")
	})
	t.Run("config: the tenant the config filled", func(t *testing.T) {
		td, _, _ := marked(t, chatFixture)
		runs, _ := markersOf(newHarness(t, allowlisted).run(td)) // tenant_id: fixture-tenant
		assert.Equal(t, []string{runID}, runs)
	})
}

// Both tiers run the normalizer (ADR-0005): the gateway's pass, with the same allowlist,
// keeps what the agent accepted and changes nothing else.
func TestTheGatewayPassKeepsTheMarkersTheAgentAccepted(t *testing.T) {
	tenants := []string{"tenant-eval"}
	agent := newHarness(t, func(c *Config) { c.TenantSource, c.TenantID, c.EvalTenants = TenantSourceAuth, "", tenants })
	td, resources, spans := marked(t, chatFixture)
	first := agent.runCtx(withAuth("tenant-eval"), td)

	gateway := newHarness(t, func(c *Config) { c.TenantID, c.EvalTenants = "", tenants })
	want, err := (&ptrace.JSONMarshaler{}).MarshalTraces(first)
	require.NoError(t, err)
	second := gateway.run(first)
	got, err := (&ptrace.JSONMarshaler{}).MarshalTraces(second)
	require.NoError(t, err)

	runs, injections := markersOf(second)
	assert.Equal(t, repeat(runID, resources), runs)
	assert.Equal(t, repeat(injectionID, spans), injections)
	assert.JSONEq(t, string(want), string(got), "the second pass is idempotent")
	assert.Empty(t, gateway.countsBy(rejectedMetric, "tenant", "attr", "reason"))

	diverged := newHarness(t, func(c *Config) { c.TenantID, c.EvalTenants = "", []string{"someone-else"} })
	runs, _ = markersOf(diverged.run(first))
	assert.Empty(t, runs, "a gateway with another allowlist drops them")
	assert.Contains(t, diverged.countsBy(rejectedMetric, "tenant", "attr", "reason"),
		"tenant-eval|aegis.eval.run.id|tenant_not_allowed")
}

func TestMarkerValuesNeverReachLabelsOrLogs(t *testing.T) {
	notAllowed := newHarness(t, func(c *Config) { c.EvalTenants = []string{"eval-runner"} })
	td, _, _ := marked(t, chatFixture)
	notAllowed.run(td)
	badPattern := newHarness(t, allowlisted)
	td = fixtureByID(t, chatFixture).Load(t)
	bad := pcommon.NewValueStr(runID + " x")
	withMarkers(td, bad, bad)
	badPattern.run(td)

	for _, h := range []*harness{notAllowed, badPattern} {
		rejected := h.countsBy(rejectedMetric, "tenant", "attr", "reason")
		require.NotEmpty(t, rejected)
		for k := range rejected {
			assert.NotContains(t, k, "canary")
		}
		require.NotZero(t, h.logs.Len(), "the first rejection is logged")
		for _, entry := range h.logs.All() {
			assert.NotContains(t, entry.Message, "canary")
			for k, v := range entry.ContextMap() {
				assert.NotContains(t, k+"="+fmt.Sprint(v), "canary")
			}
		}
	}
}

func TestRejectedTenantLabelsAreCapped(t *testing.T) {
	td, _, _ := marked(t, chatFixture)
	for range 2 {
		td.ResourceSpans().At(0).CopyTo(td.ResourceSpans().AppendEmpty())
	}
	for i, tenant := range []string{"a", "b", "c"} {
		td.ResourceSpans().At(i).Resource().Attributes().PutStr(tenantAttr, tenant)
	}
	h := newHarness(t, func(c *Config) { c.MaxLabelValues = 1 }) // one processor, one export
	h.run(td)
	tenants := map[string]bool{}
	for k := range h.countsBy(rejectedMetric, "tenant", "attr", "reason") {
		tenants[strings.SplitN(k, "|", 2)[0]] = true
	}
	assert.Equal(t, map[string]bool{"a": true, "_other": true}, tenants)
}
