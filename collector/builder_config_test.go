package collector

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.yaml.in/yaml/v3"
)

const (
	coreModules    = "go.opentelemetry.io/collector/"
	coreProviders  = "go.opentelemetry.io/collector/confmap/provider/"
	contribModules = "github.com/open-telemetry/opentelemetry-collector-contrib/"
	localModule    = "github.com/arec1b0/traceblame/collector"
)

type manifestModule struct {
	GoMod  string `yaml:"gomod"`
	Import string `yaml:"import"`
	Path   string `yaml:"path"`
}

type manifest struct {
	Receivers  []manifestModule `yaml:"receivers"`
	Processors []manifestModule `yaml:"processors"`
	Exporters  []manifestModule `yaml:"exporters"`
	Extensions []manifestModule `yaml:"extensions"`
	Connectors []manifestModule `yaml:"connectors"`
	Providers  []manifestModule `yaml:"providers"`
}

// release is the collector release line pinned in tools/versions.env.
type release struct{ beta, stable, contrib string }

func versionsEnv(t *testing.T) map[string]string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "tools", "versions.env"))
	require.NoError(t, err)
	env := map[string]string{}
	for _, line := range strings.Split(string(raw), "\n") {
		if k, v, ok := strings.Cut(line, "="); ok && !strings.HasPrefix(k, "#") {
			env[k] = v
		}
	}
	return env
}

func pinnedRelease(t *testing.T) release {
	t.Helper()
	env := versionsEnv(t)
	r := release{
		beta:    "v" + env["OCB_VERSION"],
		stable:  "v" + env["OTELCOL_CORE_STABLE_VERSION"],
		contrib: "v" + env["OTELCOL_CONTRIB_MODULES_VERSION"],
	}
	require.NotEqual(t, "v", r.beta, "OCB_VERSION missing from tools/versions.env")
	require.NotEqual(t, "v", r.stable, "OTELCOL_CORE_STABLE_VERSION missing from tools/versions.env")
	require.NotEqual(t, "v", r.contrib, "OTELCOL_CONTRIB_MODULES_VERSION missing from tools/versions.env")
	return r
}

func TestBuilderConfigPinsOneCollectorRelease(t *testing.T) {
	r := pinnedRelease(t)
	raw, err := os.ReadFile("builder-config.yaml")
	require.NoError(t, err)
	var m manifest
	require.NoError(t, yaml.Unmarshal(raw, &m))

	all := [][]manifestModule{m.Receivers, m.Processors, m.Exporters, m.Extensions, m.Connectors, m.Providers}
	var local int
	for _, group := range all {
		for _, mod := range group {
			path, version, ok := strings.Cut(mod.GoMod, " ")
			require.True(t, ok, "gomod without a version: %q", mod.GoMod)
			switch {
			case strings.HasPrefix(path, coreProviders):
				assert.Equal(t, r.stable, version, path)
			case strings.HasPrefix(path, coreModules):
				assert.Equal(t, r.beta, version, path)
			case strings.HasPrefix(path, contribModules):
				assert.Equal(t, r.contrib, version, path)
			case path == localModule:
				local++
				assert.Equal(t, "v0.0.0", version, "the local module is built from source")
				assert.Equal(t, "./", mod.Path, "the local module must point at this directory")
			default:
				t.Errorf("module outside the pinned release line: %s", mod.GoMod)
			}
		}
	}
	assert.Equal(t, 1, local, "the normalizer processor must be in the manifest")
}

var requireLine = regexp.MustCompile(`^\s*(?:require\s+)?(\S+)\s+(v\S+)`)

func TestGoModRequiresTheSameRelease(t *testing.T) {
	r := pinnedRelease(t)
	raw, err := os.ReadFile("go.mod")
	require.NoError(t, err)
	for _, line := range strings.Split(string(raw), "\n") {
		m := requireLine.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		path, version := m[1], m[2]
		switch {
		case strings.HasPrefix(path, coreModules):
			assert.Contains(t, []string{r.beta, r.stable}, version, path)
		case strings.HasPrefix(path, contribModules):
			assert.Equal(t, r.contrib, version, path)
		}
	}
}

var pinnedImage = regexp.MustCompile(`^[a-z0-9./-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$`)

func TestImagesArePinnedByDigestAndCarryTheGoVersion(t *testing.T) {
	env := versionsEnv(t)
	for _, key := range []string{"GO_BUILD_IMAGE", "DISTROLESS_STATIC_IMAGE"} {
		assert.Regexp(t, pinnedImage, env[key], key)
	}
	assert.True(t, strings.HasPrefix(env["GO_BUILD_IMAGE"], "golang:"+env["GO_VERSION"]+"-"),
		"GO_BUILD_IMAGE must carry GO_VERSION %s", env["GO_VERSION"])
}
