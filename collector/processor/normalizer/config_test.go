package normalizer

import (
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/confmap/confmaptest"
)

func TestDefaultConfig(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	assert.Equal(t, "aegis-telemetry/1", cfg.Profile)
	assert.Equal(t, 1000, cfg.MaxLabelValues)
	assert.Equal(t, 256, cfg.MaxUnmappedValueBytes)
	assert.False(t, cfg.ContentCapture)
	assert.Empty(t, cfg.TenantID)
}

func TestConfigFromYAML(t *testing.T) {
	conf, err := confmaptest.LoadConf(filepath.Join("testdata", "config.yaml"))
	require.NoError(t, err)
	cfg := createDefaultConfig().(*Config)
	require.NoError(t, conf.Unmarshal(cfg))
	require.NoError(t, cfg.Validate())
	assert.Equal(t, &Config{
		Profile: "aegis-telemetry/1", TenantID: "acme", MaxLabelValues: 50, MaxUnmappedValueBytes: 512,
	}, cfg)
}

func TestConfigValidate(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*Config)
		err    string
	}{
		{"unknown profile", func(c *Config) { c.Profile = "aegis-telemetry/2" }, "profile aegis-telemetry/2"},
		{"label cap below one", func(c *Config) { c.MaxLabelValues = 0 }, "max_label_values"},
		{"value limit below one", func(c *Config) { c.MaxUnmappedValueBytes = 0 }, "max_unmapped_value_bytes"},
		{"content capture", func(c *Config) { c.ContentCapture = true }, "ADR-0002"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := createDefaultConfig().(*Config)
			tc.mutate(cfg)
			assert.ErrorContains(t, cfg.Validate(), tc.err)
		})
	}
}
