package normalizer

import (
	"embed"
	"fmt"
	"io/fs"
	"strings"

	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/mapping"
	"github.com/arec1b0/traceblame/collector/processor/normalizer/internal/profile"
)

// Version of the normalizer and its embedded mapping tables, stamped on every span as
// aegis.normalizer.version. Bump it with every change to the mapping or redaction rules.
const Version = "0.1.0"

// The mapping tables (mappings/README.md) and the profile copies, kept byte-identical to
// schemas/profile by `traceblame-contracts gen-schema`.
//
//go:embed mappings profiles
var embedded embed.FS

// loadProfile reads the embedded profile with the given id, e.g. aegis-telemetry/1.
func loadProfile(id string) (*profile.Profile, error) {
	name, major, ok := strings.Cut(id, "/")
	if !ok {
		return nil, fmt.Errorf("profile id %q is not <name>/<major>", id)
	}
	raw, err := embedded.ReadFile(fmt.Sprintf("profiles/%s.v%s.yaml", name, major))
	if err != nil {
		return nil, fmt.Errorf("profile %s: %w", id, err)
	}
	return profile.Parse(raw)
}

func loadTables(p *profile.Profile) (*mapping.Registry, error) {
	sub, err := fs.Sub(embedded, "mappings")
	if err != nil {
		return nil, err
	}
	return mapping.Load(sub, p)
}
