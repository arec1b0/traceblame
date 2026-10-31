package normalizer

import (
	"errors"
	"fmt"

	"go.opentelemetry.io/collector/component"
)

// Where aegis.tenant.id comes from (ADR-0005).
const (
	// TenantSourceConfig: single-tenant mode. A tenant set by the shim is kept; TenantID
	// fills it when missing.
	TenantSourceConfig = "config"
	// TenantSourceAuth: multi-tenant mode, on the first authenticated hop (the agent). The
	// tenant is the authenticated client's TenantAuthAttribute and always overrides the
	// shim; without authentication the tenant is dropped and counted.
	TenantSourceAuth = "auth"
)

// Config of the normalizer processor. Thresholds are starting values, kept in config
// (CLAUDE.md hard rule 8).
type Config struct {
	// Profile is the profile the normalizer emits. One processor emits one profile; a
	// v(N)/v(N-1) migration runs two pipelines (ADR-0001).
	Profile string `mapstructure:"profile"`
	// TenantSource is config or auth.
	TenantSource string `mapstructure:"tenant_source"`
	// TenantID fills aegis.tenant.id on resources that lack it (tenant_source: config).
	// Empty: never filled.
	TenantID string `mapstructure:"tenant_id"`
	// TenantAuthAttribute is the client.Info.Auth attribute that names the tenant
	// (tenant_source: auth), e.g. basicauth's username.
	TenantAuthAttribute string `mapstructure:"tenant_auth_attribute"`
	// MaxLabelValues caps the distinct label sets each normalizer counter records; further
	// ones are counted under attr/schema_url "_other".
	MaxLabelValues int `mapstructure:"max_label_values"`
	// MaxUnmappedValueBytes: on GenAI spans, an attribute that neither the profile nor the
	// table knows is dropped when its string value is longer (fail closed, ADR-0002).
	MaxUnmappedValueBytes int `mapstructure:"max_unmapped_value_bytes"`
	// ContentCapture is the tenant opt-in to the encrypted out-of-band content copy
	// (aegis.content.ref, ADR-0002). It must stay false until the content store exists.
	ContentCapture bool `mapstructure:"content_capture"`
}

func createDefaultConfig() component.Config {
	return &Config{
		Profile:               "aegis-telemetry/1",
		TenantSource:          TenantSourceConfig,
		TenantAuthAttribute:   "username",
		MaxLabelValues:        1000,
		MaxUnmappedValueBytes: 256,
	}
}

// Validate checks the configuration.
func (c *Config) Validate() error {
	if _, ok := emitters[c.Profile]; !ok {
		return fmt.Errorf("profile %s is not supported", c.Profile)
	}
	switch c.TenantSource {
	case TenantSourceConfig:
	case TenantSourceAuth:
		if c.TenantID != "" {
			return errors.New("tenant_id is for tenant_source: config; with auth the tenant comes from the client")
		}
		if c.TenantAuthAttribute == "" {
			return errors.New("tenant_auth_attribute is required with tenant_source: auth")
		}
	default:
		return fmt.Errorf("tenant_source %q: expected config or auth", c.TenantSource)
	}
	if c.MaxLabelValues < 1 {
		return errors.New("max_label_values must be at least 1")
	}
	if c.MaxUnmappedValueBytes < 1 {
		return errors.New("max_unmapped_value_bytes must be at least 1")
	}
	if c.ContentCapture {
		return errors.New("content_capture needs the encrypted content store of ADR-0002 (Proposed), " +
			"which does not exist yet; content stays hashed or dropped")
	}
	return nil
}
