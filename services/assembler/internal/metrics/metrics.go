// Package metrics is the assembler's self-telemetry (design 1.5): Prometheus metrics on a
// separate registry and port, never routed through the analysed pipeline (CLAUDE.md rule 7).
package metrics

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
	"github.com/arec1b0/traceblame/services/assembler/internal/reconcile"
)

// Metrics implements poll.Metrics and reconcile.Reporter.
type Metrics struct {
	Registry *prometheus.Registry
	grace    time.Duration

	// The lag is computed at scrape time from the committed watermark and ClickHouse's clock, so
	// it keeps growing while steps fail or do not run at all.
	mu            sync.Mutex
	committed     map[int]int64 // owned partition -> committed watermark (epoch ms)
	offsetMs      int64         // ClickHouse's clock minus this host's, at the last reading
	haveClock     bool
	now           func() time.Time
	batch         prometheus.Histogram
	assembled     *prometheus.CounterVec
	waiting       prometheus.Counter
	skipped       *prometheus.CounterVec
	lateDelay     prometheus.Histogram
	stepDuration  prometheus.Histogram
	stepErrors    *prometheus.CounterVec
	claims        *prometheus.CounterVec
	queryDuration *prometheus.HistogramVec
	queryRows     *prometheus.CounterVec
	duplicates    prometheus.Counter
	missing       prometheus.Gauge
	orphans       prometheus.Gauge
	reconcileOK   prometheus.Gauge
	reconcileFail prometheus.Counter
	live          prometheus.Gauge
	target        prometheus.Gauge
	owned         prometheus.Gauge
	released      prometheus.Counter
}

const ns = "traceblame_assembler"

// New registers every metric. grace is added back to the watermark lag: the watermark trails
// ClickHouse's clock by the grace by design.
func New(grace time.Duration) *Metrics {
	m := &Metrics{
		Registry: prometheus.NewRegistry(), grace: grace, committed: map[int]int64{}, now: time.Now,
	}
	f := func(name, help string) prometheus.Opts { return prometheus.Opts{Namespace: ns, Name: name, Help: help} }
	h := func(name, help string, buckets []float64) prometheus.HistogramOpts {
		return prometheus.HistogramOpts{Namespace: ns, Name: name, Help: help, Buckets: buckets}
	}
	secs := []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60}
	m.batch = prometheus.NewHistogram(h("batch_traces", "Traces in one window's scan.", []float64{0, 1, 5, 10, 50, 100, 500, 1000, 5000}))
	m.assembled = prometheus.NewCounterVec(prometheus.CounterOpts(f("assembled_traces_total",
		"Traces assembled; kind=first for a new trace, again for a re-assembly (late spans or a replayed window).")), []string{"kind"})
	m.waiting = prometheus.NewCounter(prometheus.CounterOpts(f("waiting_traces_total", "Traces seen without a root that were never assembled.")))
	m.skipped = prometheus.NewCounterVec(prometheus.CounterOpts(f("skipped_traces_total", "Traces not summarised.")), []string{"reason"})
	m.lateDelay = prometheus.NewHistogram(h("late_span_delay_seconds",
		"Time between a trace's previous revision and the arrival that re-assembled it.", []float64{1, 5, 10, 30, 60, 300, 900, 3600}))
	m.stepDuration = prometheus.NewHistogram(h("step_duration_seconds", "One window of one partition, claim to commit.", secs))
	m.stepErrors = prometheus.NewCounterVec(prometheus.CounterOpts(f("step_errors_total", "Steps that did not commit.")), []string{"reason"})
	m.claims = prometheus.NewCounterVec(prometheus.CounterOpts(f("lease_claims_total",
		"Lease claims; outcome=held when another replica owns the partition.")), []string{"outcome"})
	m.queryDuration = prometheus.NewHistogramVec(h("query_duration_seconds", "ClickHouse query time.", secs), []string{"query"})
	m.queryRows = prometheus.NewCounterVec(prometheus.CounterOpts(f("query_rows_total", "Rows ClickHouse queries returned.")), []string{"query"})
	m.duplicates = prometheus.NewCounter(prometheus.CounterOpts(f("publish_duplicates_total", "Publishes the stream acked as duplicates.")))
	m.missing = prometheus.NewGauge(prometheus.GaugeOpts(f("reconcile_missing_traces",
		"Traces with a root arrival and no trace_summary row in the last reconciled hour.")))
	m.orphans = prometheus.NewGauge(prometheus.GaugeOpts(f("reconcile_orphan_traces",
		"Traces with arrivals but no root and no summary in the last reconciled hour.")))
	m.reconcileOK = prometheus.NewGauge(prometheus.GaugeOpts(f("reconcile_last_success_timestamp_seconds", "Unix time of the last successful reconciliation.")))
	m.reconcileFail = prometheus.NewCounter(prometheus.CounterOpts(f("reconcile_failures_total", "Reconciliations that failed.")))
	m.live = prometheus.NewGauge(prometheus.GaugeOpts(f("live_replicas", "Trace-assembler replicas alive, as this replica sees them (heartbeat within the lease TTL).")))
	m.target = prometheus.NewGauge(prometheus.GaugeOpts(f("target_partitions", "This replica's fair share of partitions: ceil(partitions / live replicas).")))
	m.owned = prometheus.NewGauge(prometheus.GaugeOpts(f("owned_partitions", "Partitions this replica owned in its last round.")))
	m.released = prometheus.NewCounter(prometheus.CounterOpts(f("partitions_released_total", "Partitions given up above the fair share so a joining replica can take them.")))
	m.Registry.MustRegister(lagCollector{m}, m.live, m.target, m.owned, m.released, m.batch, m.assembled, m.waiting, m.skipped, m.lateDelay,
		m.stepDuration, m.stepErrors, m.claims, m.queryDuration, m.queryRows, m.duplicates,
		m.missing, m.orphans, m.reconcileOK, m.reconcileFail)
	return m
}

var lagDesc = prometheus.NewDesc(
	ns+"_watermark_lag_seconds",
	"ClickHouse's clock minus the partition's committed watermark, for the partitions this replica owns. "+
		"The grace is part of it by design; it keeps growing while steps fail.",
	[]string{"partition"}, nil)

// lagCollector computes the lag when it is scraped.
type lagCollector struct{ m *Metrics }

func (lagCollector) Describe(ch chan<- *prometheus.Desc) { ch <- lagDesc }

func (c lagCollector) Collect(ch chan<- prometheus.Metric) {
	c.m.mu.Lock()
	defer c.m.mu.Unlock()
	if !c.m.haveClock {
		return
	}
	nowMs := c.m.now().UnixMilli() + c.m.offsetMs
	for partition, committed := range c.m.committed {
		ch <- prometheus.MustNewConstMetric(lagDesc, prometheus.GaugeValue,
			float64(nowMs-committed)/1000, strconv.Itoa(partition))
	}
}

func (m *Metrics) forget(partition int) {
	m.mu.Lock()
	delete(m.committed, partition)
	m.mu.Unlock()
}

// Step implements poll.Metrics.
func (m *Metrics) Step(partition int, r poll.Result, took time.Duration, err error) {
	if !r.Owned {
		m.claims.WithLabelValues("held").Inc()
		m.forget(partition) // only the owner reports a lag
		return
	}
	m.claims.WithLabelValues("owned").Inc()
	m.stepDuration.Observe(took.Seconds())
	m.mu.Lock()
	if r.UpperMs > 0 { // ClickHouse's now is the frontier plus the grace
		m.offsetMs = r.UpperMs + m.grace.Milliseconds() - m.now().UnixMilli()
		m.haveClock = true
	}
	if !errors.Is(err, poll.ErrLeaseLost) {
		m.committed[partition] = r.CommittedMs // also after an error: the watermark did not move
	}
	m.mu.Unlock()
	switch {
	case errors.Is(err, poll.ErrLeaseLost):
		m.stepErrors.WithLabelValues("lease_lost").Inc()
		m.forget(partition)
		return
	case err != nil:
		m.stepErrors.WithLabelValues("error").Inc()
		return
	}
	if r.Scanned {
		m.batch.Observe(float64(r.Arrivals))
	}
	m.assembled.WithLabelValues("first").Add(float64(r.Assembled))
	m.assembled.WithLabelValues("again").Add(float64(r.Reassembled))
	m.waiting.Add(float64(r.Waiting))
	m.skipped.WithLabelValues("root_missing").Add(float64(r.Skipped))
	for _, d := range r.LateDelaysMs {
		m.lateDelay.Observe(float64(d) / 1000)
	}
}

// Round implements poll.Metrics: how many replicas are alive, the fair share, and how many
// partitions this replica owns after the round.
func (m *Metrics) Round(r poll.RoundResult) {
	m.live.Set(float64(r.Live))
	m.target.Set(float64(r.Target))
	owned := 0
	for _, res := range r.Results {
		if res.Owned {
			owned++
		}
	}
	m.owned.Set(float64(owned))
	m.released.Add(float64(len(r.Released)))
	for _, part := range r.Released {
		m.forget(part)
	}
}

// Duplicate counts an ack the stream reported as a duplicate.
func (m *Metrics) Duplicate() { m.duplicates.Inc() }

// Reconciled implements reconcile.Reporter.
func (m *Metrics) Reconciled(r reconcile.Result) {
	m.missing.Set(float64(r.Missing))
	m.orphans.Set(float64(r.Orphans))
	m.reconcileOK.SetToCurrentTime()
}

// ReconcileFailed implements reconcile.Reporter.
func (m *Metrics) ReconcileFailed() { m.reconcileFail.Inc() }

// Source decorates a poll.Source with query duration and returned rows.
func (m *Metrics) Source(src poll.Source) poll.Source { return &timedSource{Source: src, m: m} }

type timedSource struct {
	poll.Source
	m *Metrics
}

func (t *timedSource) observe(query string, start time.Time, rows int) {
	t.m.queryDuration.WithLabelValues(query).Observe(time.Since(start).Seconds())
	t.m.queryRows.WithLabelValues(query).Add(float64(rows))
}

func (t *timedSource) Scan(ctx context.Context, count, partition int, w0, w1 int64) ([]poll.Arrival, error) {
	start := time.Now()
	out, err := t.Source.Scan(ctx, count, partition, w0, w1)
	t.observe("scan", start, len(out))
	return out, err
}

func (t *timedSource) Spans(ctx context.Context, ids []string, sinceMs int64) ([]poll.Span, error) {
	start := time.Now()
	out, err := t.Source.Spans(ctx, ids, sinceMs)
	t.observe("spans", start, len(out))
	return out, err
}

func (t *timedSource) Assembled(ctx context.Context, ids []string) (map[string]int64, error) {
	start := time.Now()
	out, err := t.Source.Assembled(ctx, ids)
	t.observe("assembled", start, len(out))
	return out, err
}
