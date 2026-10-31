// Package fixtures loads the golden span fixtures in collector/testdata/fixtures for Go tests.
//
// The fixtures are real OTLP/JSON exports, written by tools/fixtures/capture.py and never by
// hand (see collector/testdata/fixtures/README.md).
package fixtures

import (
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

const suffix = ".otlp.json"

// Fixture is one captured export.
type Fixture struct {
	// ID is the path below the fixtures root without the suffix, e.g.
	// "openllmetry/opentelemetry-instrumentation-openai-0.62.4/chat_basic".
	ID   string
	Path string
}

// Root is the absolute path of collector/testdata/fixtures.
func Root() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		panic("fixtures: cannot locate the source file")
	}
	return filepath.Join(filepath.Dir(file), "..", "..", "testdata", "fixtures")
}

// All returns every fixture, sorted by ID. It fails the test if there are none.
func All(tb testing.TB) []Fixture {
	tb.Helper()
	root := Root()
	var out []Fixture
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, suffix) {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		out = append(out, Fixture{ID: strings.TrimSuffix(filepath.ToSlash(rel), suffix), Path: path})
		return nil
	})
	if err != nil {
		tb.Fatalf("fixtures: %v", err)
	}
	if len(out) == 0 {
		tb.Fatalf("fixtures: none under %s", root)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// Load unmarshals the fixture into a fresh ptrace.Traces.
func (f Fixture) Load(tb testing.TB) ptrace.Traces {
	tb.Helper()
	raw, err := os.ReadFile(f.Path)
	if err != nil {
		tb.Fatalf("fixtures: %v", err)
	}
	td, err := (&ptrace.JSONUnmarshaler{}).UnmarshalTraces(raw)
	if err != nil {
		tb.Fatalf("fixtures: %s: %v", f.ID, err)
	}
	return td
}
