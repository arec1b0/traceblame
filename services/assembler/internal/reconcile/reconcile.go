// Package reconcile compares trace_arrivals with trace_summary for the last closed hour
// (ADR-0003): a trace with a root arrival and no summary is a miss, and the alert
// TraceSummaryReconciliationMiss fires on it.
package reconcile

import (
	"context"
	"log/slog"
	"time"
)

// Checker is ClickHouse's side of the reconciliation.
type Checker interface {
	Now(ctx context.Context) (int64, error)
	Reconcile(ctx context.Context, h0, h1, sinceMs int64) (missing, orphans uint64, err error)
}

// Result is one closed hour's outcome.
type Result struct {
	H0Ms, H1Ms int64
	Missing    uint64
	Orphans    uint64
}

// Reporter receives each result.
type Reporter interface {
	Reconciled(r Result)
	ReconcileFailed()
}

// Window is the latest hour that closed at least margin ago, in epoch milliseconds.
func Window(nowMs int64, margin time.Duration) (h0, h1 int64) {
	const hour = int64(time.Hour / time.Millisecond)
	h1 = (nowMs - margin.Milliseconds()) / hour * hour
	return h1 - hour, h1
}

// Once reconciles the latest closed hour. lookback bounds the trace_summary scan.
func Once(ctx context.Context, c Checker, rep Reporter, margin, lookback time.Duration) (Result, error) {
	now, err := c.Now(ctx)
	if err != nil {
		return Result{}, err
	}
	h0, h1 := Window(now, margin)
	missing, orphans, err := c.Reconcile(ctx, h0, h1, h0-lookback.Milliseconds())
	if err != nil {
		return Result{}, err
	}
	r := Result{H0Ms: h0, H1Ms: h1, Missing: missing, Orphans: orphans}
	rep.Reconciled(r)
	return r, nil
}

// Run reconciles every interval until ctx ends. A failed pass is logged and counted, never fatal.
func Run(ctx context.Context, c Checker, rep Reporter, every, margin, lookback time.Duration, log *slog.Logger) {
	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		if r, err := Once(ctx, c, rep, margin, lookback); err != nil {
			if ctx.Err() != nil {
				return
			}
			rep.ReconcileFailed()
			log.Error("reconciliation failed", "error", err.Error(), "component", "assembler")
		} else if r.Missing > 0 {
			log.Warn("traces with a root arrival have no summary", "missing", r.Missing,
				"hour_start_ms", r.H0Ms, "component", "assembler")
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}
