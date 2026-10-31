package normalizer

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/client"
)

type authData map[string]string

func (a authData) GetAttribute(name string) any {
	if v, ok := a[name]; ok {
		return v
	}
	return nil
}

func (a authData) GetAttributeNames() []string {
	names := make([]string, 0, len(a))
	for k := range a {
		names = append(names, k)
	}
	return names
}

func withAuth(username string) context.Context {
	return client.NewContext(context.Background(), client.Info{Auth: authData{"username": username}})
}

const chatFixture = "otel-genai/opentelemetry-instrumentation-openai-v2-2.4b0/chat_basic"

func tenantOf(ctx context.Context, t *testing.T, h *harness, shim string) (string, bool) {
	t.Helper()
	td := fixtureByID(t, chatFixture).Load(t)
	if shim != "" {
		td.ResourceSpans().At(0).Resource().Attributes().PutStr("aegis.tenant.id", shim)
	}
	out := h.runCtx(ctx, td)
	v, ok := out.ResourceSpans().At(0).Resource().Attributes().Get("aegis.tenant.id")
	if !ok {
		return "", false
	}
	return v.AsString(), true
}

func TestAuthTenantOverridesTheShim(t *testing.T) {
	h := newHarness(t, func(c *Config) { c.TenantID = ""; c.TenantSource = TenantSourceAuth })
	got, ok := tenantOf(withAuth("tenant-a"), t, h, "tenant-b")
	require.True(t, ok)
	assert.Equal(t, "tenant-a", got, "a shim cannot claim another tenant")
	got, _ = tenantOf(withAuth("tenant-a"), t, h, "")
	assert.Equal(t, "tenant-a", got)
}

func TestAuthTenantWithoutAuthenticationDropsTheTenant(t *testing.T) {
	h := newHarness(t, func(c *Config) { c.TenantID = ""; c.TenantSource = TenantSourceAuth })
	_, ok := tenantOf(context.Background(), t, h, "tenant-b")
	assert.False(t, ok, "never trust the shim in auth mode")
	assert.Equal(t, map[string]int64{"unknown|aegis.tenant.id": 1}, h.counts(invalidMetric, "sdk", "attr"))
}

func TestConfigTenantTrustsTheShim(t *testing.T) {
	h := newHarness(t, nil) // tenant_source: config, tenant_id: fixture-tenant
	got, _ := tenantOf(withAuth("tenant-a"), t, h, "from-shim")
	assert.Equal(t, "from-shim", got, "single-tenant mode keeps the shim value")
}

func TestTenantConfigValidation(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	cfg.TenantSource = "header"
	assert.ErrorContains(t, cfg.Validate(), "tenant_source")
	cfg = createDefaultConfig().(*Config)
	cfg.TenantSource, cfg.TenantID = TenantSourceAuth, "fixed"
	assert.ErrorContains(t, cfg.Validate(), "tenant_id")
	cfg = createDefaultConfig().(*Config)
	cfg.TenantSource, cfg.TenantAuthAttribute = TenantSourceAuth, ""
	assert.ErrorContains(t, cfg.Validate(), "tenant_auth_attribute")
}
