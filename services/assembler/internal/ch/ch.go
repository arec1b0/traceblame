// Package ch is the assembler's ClickHouse side: the watermark scan, the spans read and the
// trace_summary writes. The statements are the embedded copies of the storage contract.
package ch

import (
	"context"
	"crypto/tls"
	_ "embed"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
)

//go:embed sql/trace_arrivals_scan.sql
var scanSQL string

//go:embed sql/spans_by_trace_read.sql
var spansSQL string

//go:embed sql/trace_summary_assembled_read.sql
var assembledSQL string

const insertSummary = "INSERT INTO trace_summary (trace_id, assembler_version, revision, service, " +
	"tenant_id, root_span_id, root_status_code, start_ts, duration_ms, span_count, " +
	"error_span_count, bundle_digest, session_id)"

// Options connects the native protocol (port 9000, or 9440 with TLS).
type Options struct {
	Addr     string
	Database string
	User     string
	Password string
	TLS      bool
}

// Client implements poll.Source and poll.Summaries.
type Client struct {
	conn driver.Conn
}

var (
	_ poll.Source    = (*Client)(nil)
	_ poll.Summaries = (*Client)(nil)
)

// Open connects and pings.
func Open(ctx context.Context, o Options) (*Client, error) {
	opts := &clickhouse.Options{
		Addr: []string{o.Addr},
		Auth: clickhouse.Auth{Database: o.Database, Username: o.User, Password: o.Password},
		// The clock and the windows are ClickHouse's; sessions never carry DateTime64 time zones.
		Settings:    clickhouse.Settings{"max_execution_time": 60},
		DialTimeout: 10 * time.Second,
	}
	if o.TLS {
		opts.TLS = &tls.Config{MinVersion: tls.VersionTLS12}
	}
	conn, err := clickhouse.Open(opts)
	if err != nil {
		return nil, fmt.Errorf("ch: open: %w", err)
	}
	if err := conn.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ch: ping: %w", err)
	}
	return &Client{conn: conn}, nil
}

// Close releases the connection pool.
func (c *Client) Close() error { return c.conn.Close() }

// params sends server-side parameters ({name:Type} in the statements).
func params(ctx context.Context, kv ...string) context.Context {
	p := clickhouse.Parameters{}
	for i := 0; i+1 < len(kv); i += 2 {
		p[kv[i]] = kv[i+1]
	}
	return clickhouse.Context(ctx, clickhouse.WithParameters(p))
}

func i64(v int64) string { return strconv.FormatInt(v, 10) }

// Upper is ClickHouse's now64(3) minus the grace, in epoch milliseconds.
func (c *Client) Upper(ctx context.Context, graceMs int64) (int64, error) {
	var now int64
	if err := c.conn.QueryRow(ctx, "SELECT toUnixTimestamp64Milli(now64(3))").Scan(&now); err != nil {
		return 0, err
	}
	return now - graceMs, nil
}

// Scan runs the watermark scan (contract/trace_arrivals_scan.sql).
func (c *Client) Scan(ctx context.Context, count, partition int, w0, w1 int64) ([]poll.Arrival, error) {
	ctx = params(ctx, "w0", i64(w0), "w1", i64(w1),
		"partition_count", strconv.Itoa(count), "partition", strconv.Itoa(partition))
	rows, err := c.conn.Query(ctx, scanSQL)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var out []poll.Arrival
	for rows.Next() {
		var (
			a    poll.Arrival
			root bool
		)
		if err := rows.Scan(&a.TraceID, &root, &a.LastArrivalMs); err != nil {
			return nil, err
		}
		a.HasRoot = root
		out = append(out, a)
	}
	return out, rows.Err()
}

var hexID = regexp.MustCompile(`^[0-9a-f]{32}$`)

// idArray formats trace ids as a ClickHouse Array(String) literal. The ids come from
// trace_arrivals, but only 32 lowercase hex characters are ever put into the statement.
func idArray(ids []string) (string, error) {
	quoted := make([]string, len(ids))
	for i, id := range ids {
		if !hexID.MatchString(id) {
			return "", fmt.Errorf("ch: %q is not a trace id", id)
		}
		quoted[i] = "'" + id + "'"
	}
	return "[" + strings.Join(quoted, ",") + "]", nil
}

// Assembled returns the newest summary revision per trace that has one, of any version.
func (c *Client) Assembled(ctx context.Context, ids []string) (map[string]int64, error) {
	arr, err := idArray(ids)
	if err != nil {
		return nil, err
	}
	rows, err := c.conn.Query(params(ctx, "trace_ids", arr), assembledSQL)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	out := map[string]int64{}
	for rows.Next() {
		var (
			id  string
			rev int64
		)
		if err := rows.Scan(&id, &rev); err != nil {
			return nil, err
		}
		out[id] = rev
	}
	return out, rows.Err()
}

// Spans reads every span of the traces that started at or after sinceMs.
func (c *Client) Spans(ctx context.Context, ids []string, sinceMs int64) ([]poll.Span, error) {
	arr, err := idArray(ids)
	if err != nil {
		return nil, err
	}
	rows, err := c.conn.Query(params(ctx, "trace_ids", arr, "since", i64(sinceMs)), spansSQL)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var out []poll.Span
	for rows.Next() {
		var s poll.Span
		if err := rows.Scan(&s.TraceID, &s.SpanID, &s.ParentSpanID, &s.Service, &s.StatusCode,
			&s.TenantID, &s.BundleDigest, &s.SessionID, &s.StartMicros, &s.DurationMs); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// Write inserts summaries in one batch. A row with the same (trace_id, assembler_version,
// revision) is the same row; the ReplacingMergeTree keeps the newest revision.
func (c *Client) Write(ctx context.Context, rows []poll.Summary) error {
	batch, err := c.conn.PrepareBatch(ctx, insertSummary)
	if err != nil {
		return err
	}
	for _, r := range rows {
		if err := batch.Append(
			r.TraceID, r.AssemblerVersion, time.UnixMilli(r.RevisionMs).UTC(), r.Service,
			r.TenantID, r.RootSpanID, r.RootStatusCode, time.UnixMicro(r.StartMicros).UTC(),
			r.DurationMs, r.SpanCount, r.ErrorSpanCount, r.BundleDigest, r.SessionID,
		); err != nil {
			_ = batch.Abort()
			return err
		}
	}
	return batch.Send()
}

//go:embed sql/trace_summary_reconcile_read.sql
var reconcileSQL string

// Now is ClickHouse's clock in epoch milliseconds.
func (c *Client) Now(ctx context.Context) (int64, error) { return c.Upper(ctx, 0) }

// Reconcile compares the arrivals of the closed window [h0, h1) with trace_summary: traces with
// a root arrival and no summary, and traces with neither a root nor a summary.
func (c *Client) Reconcile(ctx context.Context, h0, h1, sinceMs int64) (missing, orphans uint64, err error) {
	ctx = params(ctx, "h0", i64(h0), "h1", i64(h1), "since", i64(sinceMs))
	err = c.conn.QueryRow(ctx, reconcileSQL).Scan(&missing, &orphans)
	return missing, orphans, err
}
