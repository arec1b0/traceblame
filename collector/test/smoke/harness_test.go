// Package smoke runs the built collector distribution end to end: OTLP/HTTP in, file
// exporter out, JSON logs and Prometheus self-telemetry on the side.
//
// The tests skip unless OTELCOL_BIN points at a built binary: `make collector-smoke`.
package smoke

import (
	"bufio"
	"bytes"
	"encoding/hex"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"text/template"
	"time"

	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

const configTemplate = `
extensions:
  health_check:
    endpoint: 127.0.0.1:%[1]d
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:%[2]d
processors:
  # Fixed limits: on WSL2, percentage limits fail to read the cgroup (Docker Desktop's
  # mountinfo entry has spaces in it).
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
    spike_limit_mib: 128
  normalizer:%[3]s
exporters:
  file:
    path: %[4]s
    format: json
service:
  extensions: [health_check]
  telemetry:
    logs:
      level: %[7]s
      encoding: json
      output_paths: [%[5]s]
      error_output_paths: [%[5]s]
    metrics:
      readers:
        - pull:
            exporter:
              prometheus:
                host: 127.0.0.1
                port: %[6]d
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, normalizer]
      exporters: [file]
`

type collector struct {
	cmd        *exec.Cmd
	exited     chan struct{} // closed when the process has exited
	waitErr    error         // set before exited is closed
	otlpURL    string
	healthURL  string
	metricsURL string
	outPath    string
	logPath    string
	stopped    bool
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	port := l.Addr().(*net.TCPAddr).Port
	require.NoError(t, l.Close())
	return port
}

// startCollector runs OTELCOL_BIN with the normalizer settings given as an indented YAML
// block (empty for the defaults) and the given log level.
func startCollector(t *testing.T, normalizerYAML, logLevel string) *collector {
	t.Helper()
	bin := os.Getenv("OTELCOL_BIN")
	if bin == "" {
		t.Skip("OTELCOL_BIN is not set: run `make collector-smoke`")
	}
	dir := t.TempDir()
	health, otlp, metrics := freePort(t), freePort(t), freePort(t)
	c := &collector{
		otlpURL:    fmt.Sprintf("http://127.0.0.1:%d/v1/traces", otlp),
		healthURL:  fmt.Sprintf("http://127.0.0.1:%d/", health),
		metricsURL: fmt.Sprintf("http://127.0.0.1:%d/metrics", metrics),
		outPath:    filepath.Join(dir, "traces.jsonl"),
		logPath:    filepath.Join(dir, "collector.log"),
	}
	cfg := fmt.Sprintf(configTemplate, health, otlp, normalizerYAML, c.outPath, c.logPath, metrics, logLevel)
	return launch(t, bin, dir, c, cfg)
}

// Ports and paths of a custom collector config (startCustom).
type Ports struct {
	Health, OTLP, Metrics int
	Out, Log, Dir         string
}

// startCustom runs OTELCOL_BIN with a config template ({{.Health}}, {{.OTLP}}, {{.Out}},
// {{.Log}}, {{.Metrics}}, {{.Dir}}); otlpURL points at an OTLP/HTTP receiver on {{.OTLP}}.
func startCustom(t *testing.T, tmpl string) (*collector, Ports) {
	t.Helper()
	return startCustomStaged(t, tmpl, nil)
}

// startCustomStaged is startCustom with stage(dir) run first, to put files under {{.Dir}}.
func startCustomStaged(t *testing.T, tmpl string, stage func(dir string)) (*collector, Ports) {
	t.Helper()
	bin := os.Getenv("OTELCOL_BIN")
	if bin == "" {
		t.Skip("OTELCOL_BIN is not set: run `make collector-smoke`")
	}
	dir := t.TempDir()
	if stage != nil {
		stage(dir)
	}
	p := Ports{
		Health: freePort(t), OTLP: freePort(t), Metrics: freePort(t),
		Out: filepath.Join(dir, "traces.jsonl"), Log: filepath.Join(dir, "collector.log"), Dir: dir,
	}
	var b strings.Builder
	require.NoError(t, template.Must(template.New("config").Parse(tmpl)).Execute(&b, p))
	c := &collector{
		otlpURL:    fmt.Sprintf("http://127.0.0.1:%d/v1/traces", p.OTLP),
		healthURL:  fmt.Sprintf("http://127.0.0.1:%d/", p.Health),
		metricsURL: fmt.Sprintf("http://127.0.0.1:%d/metrics", p.Metrics),
		outPath:    p.Out,
		logPath:    p.Log,
	}
	return launch(t, bin, dir, c, b.String()), p
}

func launch(t *testing.T, bin, dir string, c *collector, cfg string) *collector {
	t.Helper()
	cfgPath := filepath.Join(dir, "config.yaml")
	require.NoError(t, os.WriteFile(cfgPath, []byte(cfg), 0o600))

	c.cmd = exec.Command(bin, "--config", "file:"+cfgPath) //nolint:gosec // test binary from OTELCOL_BIN
	require.NoError(t, c.cmd.Start())
	c.exited = make(chan struct{})
	go func() {
		c.waitErr = c.cmd.Wait()
		close(c.exited)
	}()
	t.Cleanup(func() { c.stop(t) })

	deadline := time.Now().Add(30 * time.Second)
	for {
		resp, err := http.Get(c.healthURL) //nolint:gosec,noctx // local test endpoint
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return c
			}
		}
		select {
		case <-c.exited:
			c.stopped = true
			log, _ := os.ReadFile(c.logPath)
			t.Fatalf("collector exited (%v); log:\n%s", c.waitErr, log)
		case <-time.After(200 * time.Millisecond):
		}
		if time.Now().After(deadline) {
			log, _ := os.ReadFile(c.logPath)
			t.Fatalf("collector did not become healthy; log:\n%s", log)
		}
	}
}

// post sends one OTLP/JSON export to the OTLP/HTTP receiver.
func (c *collector) post(t *testing.T, body []byte) {
	t.Helper()
	resp, err := http.Post(c.otlpURL, "application/json", bytes.NewReader(body)) //nolint:gosec,noctx // local test endpoint
	require.NoError(t, err)
	defer func() { _ = resp.Body.Close() }()
	require.Equal(t, http.StatusOK, resp.StatusCode)
}

// metrics scrapes the Prometheus self-telemetry endpoint.
func (c *collector) metrics(t *testing.T) string {
	t.Helper()
	resp, err := http.Get(c.metricsURL) //nolint:gosec,noctx // local test endpoint
	require.NoError(t, err)
	defer func() { _ = resp.Body.Close() }()
	var b strings.Builder
	_, err = bufio.NewReader(resp.Body).WriteTo(&b)
	require.NoError(t, err)
	return b.String()
}

// stop shuts the collector down gracefully, so the file exporter flushes.
func (c *collector) stop(t *testing.T) {
	t.Helper()
	if c.stopped {
		return
	}
	c.stopped = true
	_ = c.cmd.Process.Signal(syscall.SIGTERM)
	select {
	case <-c.exited:
	case <-time.After(30 * time.Second):
		_ = c.cmd.Process.Kill()
		t.Error("collector did not stop within 30 s")
	}
}

// exported stops the collector and returns every span the file exporter wrote.
func (c *collector) exported(t *testing.T) []ptrace.Traces {
	t.Helper()
	c.stop(t)
	raw, err := os.ReadFile(c.outPath)
	require.NoError(t, err)
	var out []ptrace.Traces
	for _, line := range bytes.Split(raw, []byte("\n")) {
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		td, err := (&ptrace.JSONUnmarshaler{}).UnmarshalTraces(line)
		require.NoError(t, err)
		out = append(out, td)
	}
	return out
}

// exportedText stops the collector and returns the file exporter's output as written.
func (c *collector) exportedText(t *testing.T) string {
	t.Helper()
	c.stop(t)
	raw, err := os.ReadFile(c.outPath)
	require.NoError(t, err)
	return string(raw)
}

// logLines stops the collector and returns its log, one entry per line.
func (c *collector) logLines(t *testing.T) []string {
	t.Helper()
	c.stop(t)
	raw, err := os.ReadFile(c.logPath)
	require.NoError(t, err)
	var lines []string
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.TrimSpace(line) != "" {
			lines = append(lines, line)
		}
	}
	return lines
}

// spanDocs flattens exports into one JSON document per span, keyed by trace and span id,
// each holding the span with its resource and scope.
func spanDocs(t *testing.T, exports ...ptrace.Traces) map[string]string {
	t.Helper()
	docs := map[string]string{}
	for _, td := range exports {
		for i := 0; i < td.ResourceSpans().Len(); i++ {
			rs := td.ResourceSpans().At(i)
			for j := 0; j < rs.ScopeSpans().Len(); j++ {
				ss := rs.ScopeSpans().At(j)
				for k := 0; k < ss.Spans().Len(); k++ {
					span := ss.Spans().At(k)
					one := ptrace.NewTraces()
					ors := one.ResourceSpans().AppendEmpty()
					rs.Resource().CopyTo(ors.Resource())
					ors.SetSchemaUrl(rs.SchemaUrl())
					oss := ors.ScopeSpans().AppendEmpty()
					ss.Scope().CopyTo(oss.Scope())
					oss.SetSchemaUrl(ss.SchemaUrl())
					span.CopyTo(oss.Spans().AppendEmpty())
					b, err := (&ptrace.JSONMarshaler{}).MarshalTraces(one)
					require.NoError(t, err)
					tid, sid := span.TraceID(), span.SpanID()
					docs[hex.EncodeToString(tid[:])+"/"+hex.EncodeToString(sid[:])] = string(b)
				}
			}
		}
	}
	return docs
}
