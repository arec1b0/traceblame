package normalizer

import (
	"errors"
	"fmt"

	"go.opentelemetry.io/collector/component"
)

// Config of the normalizer processor. Thresholds are starting values, kept in config
// (CLAUDE.md hard rule 8).
type Config struct {
	// Profile is the profile the normalizer emits. One processor emits one profile; a
	// v(N)/v(N-1) migration runs two pipelines (ADR-0001).
	Profile string `mapstructure:"profile"`
	// TenantID fills aegis.tenant.id on resources that lack it. Empty: never filled.
	TenantID string `mapstructure:"tenant_id"`
	// MaxLabelValues caps the distinct label sets each normalizer counter records; further
	// ones are counted under attr/schema_url "_other".
	MaxLabelValues int `mapstructure:"max_label_values"`
}

func createDefaultConfig() component.Config {
	return &Config{Profile: "aegis-telemetry/1", MaxLabelValues: 1000}
}

// Validate checks the configuration.
func (c *Config) Validate() error {
	if _, ok := emitters[c.Profile]; !ok {
		return fmt.Errorf("profile %s is not supported", c.Profile)
	}
	if c.MaxLabelValues < 1 {
		return errors.New("max_label_values must be at least 1")
	}
	return nil
}
