// Package poll is the trace-assembler's loop (ADR-0003): one window of the arrival watermark
// per step, summaries written, events published, the watermark advanced last.
package poll

import (
	"context"
	"errors"
	"fmt"
)

// Arrival is one trace's row of the watermark scan.
type Arrival struct {
	TraceID       string
	HasRoot       bool
	LastArrivalMs int64 // max(ingested_at) of the trace's rows in the window
}

// Span holds what the summary needs of a stored span; never its content.
type Span struct {
	TraceID      string
	SpanID       string
	ParentSpanID string // empty for the root
	Service      string
	StatusCode   string
	TenantID     string
	BundleDigest string
	SessionID    string
	StartMicros  int64
	DurationMs   float64
}

// Summary is one trace_summary row.
type Summary struct {
	TraceID          string
	AssemblerVersion string
	RevisionMs       int64
	Service          string
	TenantID         string
	RootSpanID       string
	RootStatusCode   string
	StartMicros      int64
	DurationMs       float64
	SpanCount        uint32
	ErrorSpanCount   uint32
	BundleDigest     string
	SessionID        string
}

// Event is the trace.completed message (schemas/events/trace.completed.v1.schema.json).
type Event struct {
	TraceID          string `json:"trace_id"`
	Revision         int64  `json:"revision"`
	SpanCount        uint32 `json:"span_count"`
	AssemblerVersion string `json:"assembler_version"`
}

// MsgID is the JetStream Nats-Msg-Id: the same revision of a trace is the same message.
func (e Event) MsgID() string {
	return fmt.Sprintf("%s/%s/%d", e.TraceID, e.AssemblerVersion, e.Revision)
}

// Source reads ClickHouse. Every method uses ClickHouse's clock and tables only.
type Source interface {
	// Upper is now64(3) - grace in epoch milliseconds, on ClickHouse's clock.
	Upper(ctx context.Context, graceMs int64) (int64, error)
	// Scan is one watermark scan of the window (w0, w1] for one partition of partitionCount.
	Scan(ctx context.Context, partitionCount, partition int, w0, w1 int64) ([]Arrival, error)
	// Assembled returns, for the traces of ids that have a trace_summary row of any version,
	// the revision (epoch ms) of the newest one.
	Assembled(ctx context.Context, ids []string) (map[string]int64, error)
	// Spans reads every span of the traces whose start is at or after sinceMs.
	Spans(ctx context.Context, ids []string, sinceMs int64) ([]Span, error)
}

// Summaries writes trace_summary rows. A row with the same key and revision is the same row.
type Summaries interface {
	Write(ctx context.Context, rows []Summary) error
}

// Publisher publishes with the stream's publish-ack and Nats-Msg-Id = Event.MsgID.
type Publisher interface {
	Publish(ctx context.Context, e Event) error
}

// Lease is a replica's claim on one partition. Epoch fences the watermark advance.
type Lease struct {
	Partition   int
	Epoch       int64
	WatermarkMs int64
}

// Watermarks is the per-partition state in Postgres (assembler_partitions).
type Watermarks interface {
	// Init creates the partitions of partitionCount that do not exist, at initialMs, or at the
	// smallest watermark of the most recent other partition count when there is one.
	Init(ctx context.Context, partitionCount int, initialMs int64) error
	// Claim takes or renews the lease of a partition; ok is false while another replica holds it.
	Claim(ctx context.Context, partitionCount, partition int, owner string) (l Lease, ok bool, err error)
	// Advance moves the watermark from l.WatermarkMs to newMs if l.Epoch is still current.
	Advance(ctx context.Context, partitionCount int, l Lease, owner string, newMs int64) (ok bool, err error)
	// Heartbeat registers the replica as alive and returns how many replicas are (itself
	// included): those that heartbeated within the lease TTL.
	Heartbeat(ctx context.Context, owner string) (live int, err error)
	// Release gives a partition up (no owner, watermark kept) if l.Epoch is still the owner's.
	Release(ctx context.Context, partitionCount int, l Lease, owner string) error
}

// ErrLeaseLost means a newer owner fenced this replica's watermark advance; the window's
// outputs are idempotent, so the new owner redoes it.
var ErrLeaseLost = errors.New("poll: lease lost, watermark not advanced")
