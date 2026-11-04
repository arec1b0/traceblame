package metrics

import (
	"context"
	"errors"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
	"github.com/arec1b0/traceblame/services/assembler/internal/reconcile"
)

// lagOf reads the exported lag of one partition; ok is false when there is no series.
func lagOf(t *testing.T, m *Metrics, partition string) (lag float64, ok bool) {
	t.Helper()
	families, err := m.Registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range families {
		if f.GetName() != "traceblame_assembler_watermark_lag_seconds" {
			continue
		}
		for _, metric := range f.GetMetric() {
			for _, l := range metric.GetLabel() {
				if l.GetName() == "partition" && l.GetValue() == partition {
					return metric.GetGauge().GetValue(), true
				}
			}
		}
	}
	return 0, false
}

// alertThreshold is the number in AssemblerWatermarkLagHigh's expression, read from the rule file.
func alertThreshold(t *testing.T) float64 {
	t.Helper()
	raw, err := os.ReadFile("../../../../deploy/alerts/traceblame-assembler.rules.yml")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?m)^\s*expr:\s*traceblame_assembler_watermark_lag_seconds\s*>\s*(\d+)\s*$`).FindSubmatch(raw)
	if m == nil {
		t.Fatal("AssemblerWatermarkLagHigh no longer compares traceblame_assembler_watermark_lag_seconds with a number")
	}
	v, _ := strconv.ParseFloat(string(m[1]), 64)
	return v
}

type clock struct{ at time.Time }

func (c *clock) now() time.Time          { return c.at }
func (c *clock) advance(d time.Duration) { c.at = c.at.Add(d) }

func TestTheLagIsClickHousesNowMinusTheCommittedWatermark(t *testing.T) {
	c := &clock{at: time.Unix(1_000, 0)}
	m := New(5 * time.Second)
	m.now = c.now
	// ClickHouse's clock reads 100 s; the frontier (now - grace) is 95 s; the commit reached 90 s.
	m.Step(1, poll.Result{
		Owned: true, UpperMs: 95_000, W1: 90_000, CommittedMs: 90_000, Scanned: true, Arrivals: 4,
		Assembled: 2, Reassembled: 1, Waiting: 1, Skipped: 1, LateDelaysMs: []int64{12_000},
	}, 40*time.Millisecond, nil)

	if lag, ok := lagOf(t, m, "1"); !ok || lag != 10 {
		t.Fatalf("lag = %v (%v), want 100 s - 90 s", lag, ok)
	}
	if testutil.ToFloat64(m.assembled.WithLabelValues("first")) != 2 || testutil.ToFloat64(m.assembled.WithLabelValues("again")) != 1 {
		t.Fatal("assembled counters")
	}
	if testutil.ToFloat64(m.waiting) != 1 || testutil.ToFloat64(m.skipped.WithLabelValues("root_missing")) != 1 {
		t.Fatal("waiting / skipped")
	}
	if n, err := testutil.GatherAndCount(m.Registry, "traceblame_assembler_late_span_delay_seconds"); err != nil || n == 0 {
		t.Fatalf("late span delay: %d, %v", n, err)
	}
}

func TestAStepThatKeepsFailingMakesTheLagGrowUntilTheAlertFires(t *testing.T) {
	threshold := alertThreshold(t)
	c := &clock{at: time.Unix(1_000, 0)}
	m := New(5 * time.Second)
	m.now = c.now

	// Healthy: committed up to the frontier, 5 s behind ClickHouse's clock.
	m.Step(0, poll.Result{Owned: true, UpperMs: 95_000, W1: 95_000, CommittedMs: 95_000, Scanned: true}, time.Millisecond, nil)
	if lag, _ := lagOf(t, m, "0"); lag != 5 || lag > threshold {
		t.Fatalf("a caught-up partition is %v s behind", lag)
	}

	// Every later step fails at the frontier: its window ends at now - grace, the watermark stays.
	for i := 1; i <= 4; i++ {
		c.advance(time.Minute)
		upper := int64(95_000 + i*60_000)
		m.Step(0, poll.Result{Owned: true, UpperMs: upper, W1: upper, CommittedMs: 95_000}, time.Millisecond, errors.New("boom"))
	}
	lag, ok := lagOf(t, m, "0")
	if !ok || lag != 245 {
		t.Fatalf("lag = %v (%v), want 245 s: ClickHouse now (340 s) - committed (95 s)", lag, ok)
	}
	if lag <= threshold {
		t.Fatalf("a partition that has not moved for 4 minutes is %v s behind: AssemblerWatermarkLagHigh (> %v) must fire", lag, threshold)
	}
}

func TestTheLagKeepsGrowingWhenTheClickHouseClockFailsToo(t *testing.T) {
	c := &clock{at: time.Unix(1_000, 0)}
	m := New(5 * time.Second)
	m.now = c.now
	m.Step(0, poll.Result{Owned: true, UpperMs: 95_000, W1: 95_000, CommittedMs: 95_000, Scanned: true}, time.Millisecond, nil)
	c.advance(3 * time.Minute)
	// ClickHouse does not answer (no UpperMs): the clock offset of the last reading is used.
	m.Step(0, poll.Result{Owned: true, CommittedMs: 95_000}, time.Millisecond, errors.New("clickhouse down"))
	if lag, _ := lagOf(t, m, "0"); lag != 185 {
		t.Fatalf("lag = %v, want 185 s", lag)
	}
	c.advance(time.Minute) // and with no step at all (a wedged replica) it still grows at scrape time
	if lag, _ := lagOf(t, m, "0"); lag != 245 {
		t.Fatalf("lag = %v, want 245 s", lag)
	}
}

func lagSeries(t *testing.T, m *Metrics) int {
	t.Helper()
	n, err := testutil.GatherAndCount(m.Registry, "traceblame_assembler_watermark_lag_seconds")
	if err != nil {
		t.Fatal(err)
	}
	return n
}

func TestOnlyTheOwnerOfAPartitionReportsItsLag(t *testing.T) {
	owned := poll.Result{Owned: true, UpperMs: 100_000, W1: 90_000, CommittedMs: 90_000, Scanned: true}

	// The lease is lost to another replica: the next step finds it held elsewhere.
	m := New(time.Second)
	m.Step(1, owned, time.Millisecond, nil)
	if lagSeries(t, m) != 1 {
		t.Fatal("the owner reports the lag")
	}
	m.Step(1, poll.Result{}, 0, nil)
	if got := lagSeries(t, m); got != 0 {
		t.Fatalf("a replica that lost the lease still exports %d lag series", got)
	}

	// The commit is fenced (a takeover happened during the step).
	m = New(time.Second)
	m.Step(0, owned, time.Millisecond, poll.ErrLeaseLost)
	if got := lagSeries(t, m); got != 0 {
		t.Fatalf("a fenced step leaves %d lag series", got)
	}

	// The replica gives the partition up for a fair share and never steps it again.
	m = New(time.Second)
	m.Step(2, owned, time.Millisecond, nil)
	m.Step(3, owned, time.Millisecond, nil)
	m.Round(poll.RoundResult{Live: 2, Target: 1, Released: []int{3}})
	if got := lagSeries(t, m); got != 1 {
		t.Fatalf("after releasing partition 3 only partition 2 remains, got %d series", got)
	}
}

func TestAStepThatDoesNotCommitIsCountedByReason(t *testing.T) {
	m := New(time.Second)
	m.Step(0, poll.Result{Owned: true}, time.Millisecond, poll.ErrLeaseLost)
	m.Step(0, poll.Result{Owned: true}, time.Millisecond, errors.New("down"))
	m.Step(0, poll.Result{}, 0, nil) // not the owner
	if testutil.ToFloat64(m.stepErrors.WithLabelValues("lease_lost")) != 1 ||
		testutil.ToFloat64(m.stepErrors.WithLabelValues("error")) != 1 ||
		testutil.ToFloat64(m.claims.WithLabelValues("held")) != 1 {
		t.Fatal("step error / claim counters")
	}
}

type stubSource struct{ poll.Source }

func (stubSource) Scan(context.Context, int, int, int64, int64) ([]poll.Arrival, error) {
	return []poll.Arrival{{TraceID: "a"}, {TraceID: "b"}}, nil
}
func (stubSource) Spans(context.Context, []string, int64) ([]poll.Span, error) {
	return []poll.Span{{}, {}, {}}, nil
}
func (stubSource) Assembled(context.Context, []string) (map[string]int64, error) {
	return map[string]int64{"a": 1}, nil
}

func TestTheDecoratedSourceCountsRowsAndTimesEachQuery(t *testing.T) {
	m := New(time.Second)
	src := m.Source(stubSource{})
	_, _ = src.Scan(context.Background(), 1, 0, 0, 1)
	_, _ = src.Spans(context.Background(), nil, 0)
	_, _ = src.Assembled(context.Background(), nil)
	for q, want := range map[string]float64{"scan": 2, "spans": 3, "assembled": 1} {
		if got := testutil.ToFloat64(m.queryRows.WithLabelValues(q)); got != want {
			t.Errorf("rows[%s] = %v, want %v", q, got, want)
		}
	}
	if n, _ := testutil.GatherAndCount(m.Registry, "traceblame_assembler_query_duration_seconds"); n != 3 {
		t.Fatalf("duration series = %d", n)
	}
}

func TestReconciliationSetsTheGauges(t *testing.T) {
	m := New(time.Second)
	m.Reconciled(reconcile.Result{Missing: 3, Orphans: 2})
	m.ReconcileFailed()
	m.Duplicate()
	if testutil.ToFloat64(m.missing) != 3 || testutil.ToFloat64(m.orphans) != 2 ||
		testutil.ToFloat64(m.reconcileFail) != 1 || testutil.ToFloat64(m.duplicates) != 1 {
		t.Fatal("gauges")
	}
	if testutil.ToFloat64(m.reconcileOK) == 0 {
		t.Fatal("last success must be stamped")
	}
}

func TestEveryMetricIsUnderTheAssemblerNamespace(t *testing.T) {
	m := New(time.Second)
	m.Reconciled(reconcile.Result{})
	m.Step(0, poll.Result{Owned: true, UpperMs: 1, Scanned: true}, time.Millisecond, nil)
	families, err := m.Registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range families {
		if !strings.HasPrefix(f.GetName(), "traceblame_assembler_") {
			t.Errorf("%s is outside the namespace", f.GetName())
		}
	}
}

func TestAShrunkWindowIsCountedAndReportedPerPartition(t *testing.T) {
	m := New(time.Second)
	m.Step(0, poll.Result{Owned: true, WindowMs: 30_000, Shrank: true, CommittedMs: 1}, time.Millisecond, errors.New("timeout"))
	m.Step(0, poll.Result{Owned: true, WindowMs: 15_000, Shrank: true, CommittedMs: 1}, time.Millisecond, errors.New("timeout"))
	m.Step(1, poll.Result{Owned: true, WindowMs: 60_000, CommittedMs: 1}, time.Millisecond, nil)
	if got := testutil.ToFloat64(m.shrinks); got != 2 {
		t.Fatalf("shrinks = %v, want 2", got)
	}
	if got := testutil.ToFloat64(m.window.WithLabelValues("0")); got != 15 {
		t.Fatalf("window of partition 0 = %v s, want 15", got)
	}
	if got := testutil.ToFloat64(m.window.WithLabelValues("1")); got != 60 {
		t.Fatalf("window of partition 1 = %v s, want 60", got)
	}
	m.Step(0, poll.Result{}, 0, nil) // lease gone
	if n, _ := testutil.GatherAndCount(m.Registry, "traceblame_assembler_window_seconds"); n != 1 {
		t.Fatalf("the window series of a partition this replica no longer owns must go: %d left", n)
	}
}
