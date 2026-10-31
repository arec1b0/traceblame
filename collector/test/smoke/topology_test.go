package smoke

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha1" //nolint:gosec // htpasswd {SHA} format of the test user, not a security boundary
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/pem"
	"math/big"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	"github.com/arec1b0/traceblame/collector/internal/fixtures"
)

func repoPath(parts ...string) string {
	return filepath.Join(append([]string{fixtures.Root(), "..", "..", ".."}, parts...)...)
}

func readEnv(t *testing.T, path string) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(path))
	require.NoError(t, err)
	var out []string
	for _, line := range bytes.Split(raw, []byte("\n")) {
		if len(line) > 0 && line[0] != '#' {
			out = append(out, string(line))
		}
	}
	return out
}

// The cluster configs of deploy/kind are valid for this distribution (memory limits in MiB
// merged over them, as WSL2 cannot read cgroup percentages).
func TestTheKindConfigsValidate(t *testing.T) {
	bin := os.Getenv("OTELCOL_BIN")
	if bin == "" {
		t.Skip("OTELCOL_BIN is not set: run `make collector-smoke`")
	}
	overlay := filepath.Join("testdata", "memory-limiter-mib.yaml")
	collectorDir := repoPath("deploy", "kind", "collector")
	env := append(os.Environ(), readEnv(t, filepath.Join(collectorDir, "latency-thresholds.env"))...)
	env = append(env, "CLICKHOUSE_USER=u", "CLICKHOUSE_PASSWORD=p", "POD_NAME=otel-gateway-0")
	for name, files := range map[string][]string{
		"agent":         {"agent.yaml"},
		"gateway":       {"gateway.yaml"},
		"gateway+bench": {"gateway.yaml", "gateway-bench.yaml"},
	} {
		t.Run(name, func(t *testing.T) {
			args := []string{"validate"}
			for _, f := range files {
				args = append(args, "--config="+filepath.Join(collectorDir, f))
			}
			cmd := exec.Command(bin, append(args, "--config="+overlay)...) //nolint:gosec // test binary
			cmd.Env = env
			out, err := cmd.CombinedOutput()
			assert.NoError(t, err, "%s", out)
		})
	}
}

const agentConfig = `
extensions:
  health_check:
    endpoint: 127.0.0.1:{{.Health}}
  basicauth/apps:
    htpasswd:
      inline: |
        tenant-a:{SHA}` + "__HASH__" + `
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{{.OTLP}}
        auth:
          authenticator: basicauth/apps
processors:
  normalizer:
    tenant_source: auth
exporters:
  file:
    path: {{.Out}}
    format: json
service:
  extensions: [health_check, basicauth/apps]
  telemetry:
    metrics: {level: none}
    logs: {output_paths: [{{.Log}}], error_output_paths: [{{.Log}}]}
  pipelines:
    traces:
      receivers: [otlp]
      processors: [normalizer]
      exporters: [file]
`

func shaPassword(pw string) string {
	sum := sha1.Sum([]byte(pw)) //nolint:gosec // htpasswd {SHA}
	return base64.StdEncoding.EncodeToString(sum[:])
}

// ADR-0005: the agent names the tenant after the authenticated user; a shim value that
// claims another tenant does not survive, and unauthenticated requests are refused.
func TestTheAgentTakesTheTenantFromTheCredentials(t *testing.T) {
	cfg := bytes.ReplaceAll([]byte(agentConfig), []byte("__HASH__"), []byte(shaPassword("secret-a")))
	c, _ := startCustom(t, string(cfg))

	td := fixtures.All(t)[0].Load(t)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("aegis.tenant.id", "tenant-b")
	body, err := (&ptrace.JSONMarshaler{}).MarshalTraces(td)
	require.NoError(t, err)

	post := func(user, pw string) int {
		req, err := http.NewRequest(http.MethodPost, c.otlpURL, bytes.NewReader(body)) //nolint:noctx // local test
		require.NoError(t, err)
		req.Header.Set("Content-Type", "application/json")
		if user != "" {
			req.SetBasicAuth(user, pw)
		}
		resp, err := http.DefaultClient.Do(req)
		require.NoError(t, err)
		_ = resp.Body.Close()
		return resp.StatusCode
	}
	assert.Equal(t, http.StatusUnauthorized, post("", ""))
	assert.Equal(t, http.StatusUnauthorized, post("tenant-a", "wrong"))
	assert.Equal(t, http.StatusOK, post("tenant-a", "secret-a"))

	exported := c.exported(t)
	require.Len(t, exported, 1)
	tenant, ok := exported[0].ResourceSpans().At(0).Resource().Attributes().Get("aegis.tenant.id")
	require.True(t, ok)
	assert.Equal(t, "tenant-a", tenant.Str())
}

type pki struct {
	dir                    string
	ca, server, client     string // cert paths
	serverKey, clientKey   string
	caPool                 *x509.CertPool
	clientPair, strangerKP tls.Certificate
}

func newPKI(t *testing.T) pki {
	t.Helper()
	dir := t.TempDir()
	write := func(name string, typ string, der []byte) string {
		path := filepath.Join(dir, name)
		require.NoError(t, os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: der}), 0o600))
		return path
	}
	key := func() *ecdsa.PrivateKey {
		k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		require.NoError(t, err)
		return k
	}
	serial := int64(1)
	issue := func(tmpl *x509.Certificate, parent *x509.Certificate, pub, signer any) []byte {
		serial++
		tmpl.SerialNumber = big.NewInt(serial)
		tmpl.NotBefore, tmpl.NotAfter = time.Now().Add(-time.Hour), time.Now().Add(time.Hour)
		der, err := x509.CreateCertificate(rand.Reader, tmpl, parent, pub, signer)
		require.NoError(t, err)
		return der
	}
	caKey := key()
	caTmpl := &x509.Certificate{Subject: pkix.Name{CommonName: "agents-ca"}, IsCA: true,
		BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER := issue(caTmpl, caTmpl, &caKey.PublicKey, caKey)
	caCert, err := x509.ParseCertificate(caDER)
	require.NoError(t, err)
	marshal := func(k *ecdsa.PrivateKey) []byte {
		der, err := x509.MarshalECPrivateKey(k)
		require.NoError(t, err)
		return der
	}
	srvKey, cliKey, strangerKey := key(), key(), key()
	srvDER := issue(&x509.Certificate{Subject: pkix.Name{CommonName: "otel-gateway"},
		DNSNames: []string{"otel-gateway.observability.svc"}, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}},
		caCert, &srvKey.PublicKey, caKey)
	cliDER := issue(&x509.Certificate{Subject: pkix.Name{CommonName: "otel-agent"},
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}, caCert, &cliKey.PublicKey, caKey)
	strangerTmpl := &x509.Certificate{Subject: pkix.Name{CommonName: "stranger"}, IsCA: true,
		BasicConstraintsValid: true, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}}
	strangerDER := issue(strangerTmpl, strangerTmpl, &strangerKey.PublicKey, strangerKey)

	p := pki{dir: dir, caPool: x509.NewCertPool()}
	p.ca = write("ca.crt", "CERTIFICATE", caDER)
	p.server, p.serverKey = write("tls.crt", "CERTIFICATE", srvDER), write("tls.key", "EC PRIVATE KEY", marshal(srvKey))
	p.client, p.clientKey = write("agent.crt", "CERTIFICATE", cliDER), write("agent.key", "EC PRIVATE KEY", marshal(cliKey))
	p.caPool.AddCert(caCert)
	p.clientPair, err = tls.LoadX509KeyPair(p.client, p.clientKey)
	require.NoError(t, err)
	p.strangerKP, err = tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: strangerDER}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: marshal(strangerKey)}))
	require.NoError(t, err)
	return p
}

const gatewayConfig = `
extensions:
  health_check:
    endpoint: 127.0.0.1:{{.Health}}
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 127.0.0.1:{{.OTLP}}
        tls:
          cert_file: {{.Dir}}/pki/tls.crt
          key_file: {{.Dir}}/pki/tls.key
          client_ca_file: {{.Dir}}/pki/ca.crt
exporters:
  file:
    path: {{.Out}}
    format: json
service:
  extensions: [health_check]
  telemetry:
    metrics: {level: none}
    logs: {output_paths: [{{.Log}}], error_output_paths: [{{.Log}}]}
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [file]
`

// ADR-0005: the gateway accepts only agents, i.e. clients with a certificate of the agents'
// CA; anything else fails the TLS handshake.
func TestTheGatewayAcceptsOnlyAgentCertificates(t *testing.T) {
	if os.Getenv("OTELCOL_BIN") == "" {
		t.Skip("OTELCOL_BIN is not set: run `make collector-smoke`")
	}
	p := newPKI(t)
	// startCustom's temp dir is created inside; stage the PKI where {{.Dir}}/pki points.
	stage := func(dir string) {
		require.NoError(t, os.MkdirAll(filepath.Join(dir, "pki"), 0o700))
		for _, f := range []string{"ca.crt", "tls.crt", "tls.key"} {
			raw, err := os.ReadFile(filepath.Clean(filepath.Join(p.dir, f)))
			require.NoError(t, err)
			require.NoError(t, os.WriteFile(filepath.Join(dir, "pki", f), raw, 0o600)) //nolint:gosec // fixed test PKI file names
		}
	}
	c, ports := startCustomStaged(t, gatewayConfig, stage)

	export := func(cert *tls.Certificate) error {
		cfg := &tls.Config{RootCAs: p.caPool, ServerName: "otel-gateway.observability.svc", MinVersion: tls.VersionTLS12}
		if cert != nil {
			cfg.Certificates = []tls.Certificate{*cert}
		}
		conn, err := grpc.NewClient("127.0.0.1:"+strconv.Itoa(ports.OTLP), grpc.WithTransportCredentials(credentials.NewTLS(cfg)))
		require.NoError(t, err)
		defer func() { _ = conn.Close() }()
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, err = ptraceotlp.NewGRPCClient(conn).Export(ctx, ptraceotlp.NewExportRequestFromTraces(fixtures.All(t)[0].Load(t)))
		return err
	}
	assert.Error(t, export(nil), "no client certificate")
	assert.Error(t, export(&p.strangerKP), "a certificate of another CA")
	require.NoError(t, export(&p.clientPair), "an agent certificate")
	assert.Equal(t, 1, len(c.exported(t)))
}
