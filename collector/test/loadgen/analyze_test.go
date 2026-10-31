package main

import (
	"bytes"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func sampleReport() Report {
	return Report{
		Environment:  map[string]string{"date": "2026-10-31", "run id": "abc12345"},
		Load:         []Summary{{Pod: 0, Tenant: "tenant-a", Rate: 100, Traces: 10, SpansPer: 4, ErrorEvery: 2}},
		Phases:       map[string]*PhaseStats{"steady": {Traces: 5}, "scale 2->4": {Traces: 3, Split: 1}},
		Latency:      map[string]LatencyStat{"bench-openinference": {ThresholdMS: 4704, Traces: 100, Slow: 5, SlowStored: 4, Share: 0.05}},
		Hard:         map[string]bool{},
		DecisionWait: "30s",
	}
}

func TestTheReportNotesTheStressProfileAndLinksIncompleteSlowTraces(t *testing.T) {
	md := markdown(sampleReport(), []string{"scale 2->4"})
	assert.Contains(t, md, "error every 2nd trace = stress profile, not expected production ratio")
	latency := md[strings.Index(md, "## Latency policy"):]
	assert.Contains(t, latency, "https://github.com/arec1b0/traceblame/issues/38")
	assert.Contains(t, latency, "not fully stored")
	assert.Less(t, strings.Index(md, "stress profile"), strings.Index(md, "## Hard criteria"), "under Load")
}

func TestTheEnvironmentTableShowsTheCommitAndWhetherTheTreeWasDirty(t *testing.T) {
	r := sampleReport()
	r.Environment["commit"] = "c127f49"
	r.Environment["dirty"] = "false"
	md := markdown(r, nil)
	assert.Contains(t, md, "| commit | c127f49 |")
	assert.Contains(t, md, "| dirty | false |")
}

func git(t *testing.T, dir string, args ...string) {
	t.Helper()
	// Per-invocation settings of a throwaway repository; the user's git config is not touched.
	base := []string{"-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"}
	cmd := exec.Command("git", append(base, args...)...) //nolint:gosec // test
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	require.NoError(t, err, "%s", out)
}

func TestGitStateReportsTheHeadAndAnyChangeOutsideTheReports(t *testing.T) {
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	require.NoError(t, os.WriteFile(filepath.Join(dir, "a.txt"), []byte("a"), 0o600))
	git(t, dir, "add", "a.txt")
	git(t, dir, "commit", "-q", "-m", "init")
	head, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").Output() //nolint:gosec // test
	require.NoError(t, err)

	commit, dirty := gitState(dir)
	assert.Equal(t, strings.TrimSpace(string(head)), commit)
	assert.Equal(t, "false", dirty)

	// Reports the run itself writes do not make the tree dirty.
	require.NoError(t, os.MkdirAll(filepath.Join(dir, "evals", "reports"), 0o750))
	require.NoError(t, os.WriteFile(filepath.Join(dir, "evals", "reports", "r.md"), []byte("r"), 0o600))
	_, dirty = gitState(dir)
	assert.Equal(t, "false", dirty)

	require.NoError(t, os.WriteFile(filepath.Join(dir, "a.txt"), []byte("changed"), 0o600))
	_, dirty = gitState(dir)
	assert.Equal(t, "true", dirty, "a modified tracked file")

	git(t, dir, "checkout", "--", "a.txt")
	require.NoError(t, os.WriteFile(filepath.Join(dir, "new.go"), []byte("x"), 0o600))
	_, dirty = gitState(dir)
	assert.Equal(t, "true", dirty, "an untracked file")
}

func TestGitStateOutsideARepositoryIsUnknown(t *testing.T) {
	commit, dirty := gitState(t.TempDir())
	assert.Equal(t, "unknown", commit)
	assert.Equal(t, "unknown", dirty)
}

func TestRenderRegeneratesTheMarkdownFromTheJSONWithExtraEnvironment(t *testing.T) {
	dir := t.TempDir()
	raw, err := json.Marshal(sampleReport())
	require.NoError(t, err)
	in := filepath.Join(dir, "in.json")
	require.NoError(t, os.WriteFile(in, raw, 0o600))

	var out bytes.Buffer
	require.NoError(t, render([]string{
		"--report", in, "--out", dir, "--name", "again",
		"--set", "commit=c127f49 (inferred)", "--set", "dirty=not recorded",
	}, &out))

	md, err := os.ReadFile(filepath.Join(dir, "again.md")) //nolint:gosec // test
	require.NoError(t, err)
	assert.Contains(t, string(md), "| commit | c127f49 (inferred) |")
	assert.Contains(t, string(md), "| dirty | not recorded |")
	assert.Contains(t, string(md), "| run id | abc12345 |", "the run's own environment is kept")
	assert.Less(t, strings.Index(string(md), "scale 2->4"), strings.Index(string(md), "## Latency"), "transitions listed")

	js, err := os.ReadFile(filepath.Join(dir, "again.json")) //nolint:gosec // test
	require.NoError(t, err)
	var back Report
	require.NoError(t, json.Unmarshal(js, &back))
	assert.Equal(t, "c127f49 (inferred)", back.Environment["commit"])
	assert.Equal(t, 100.0, back.Load[0].Rate, "numbers come from the JSON unchanged")
}

func TestRenderRefusesAMalformedSetFlag(t *testing.T) {
	dir := t.TempDir()
	raw, _ := json.Marshal(sampleReport())
	in := filepath.Join(dir, "in.json")
	require.NoError(t, os.WriteFile(in, raw, 0o600))
	assert.Error(t, render([]string{"--report", in, "--out", dir, "--set", "nokey"}, &bytes.Buffer{}))
}
