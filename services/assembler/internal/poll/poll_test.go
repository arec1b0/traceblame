package poll

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"
)

const (
	second = int64(1000)
	grace  = 5 * second
)

type rig struct {
	ch *fakeCH
	st *fakeStream
	wm *fakeWatermarks
	p  *Poller
}

func newRig(t *testing.T, mutate ...func(*Config)) *rig {
	t.Helper()
	cfg := Config{
		Owner: "a", PartitionCount: 1, Grace: 5 * time.Second, MaxWindow: 60 * time.Second,
		LeaseTTL: 30 * time.Second, BatchSize: 100, Lookback: 48 * time.Hour,
		AssemblerVersion: "1.0.0", PollInterval: 2 * time.Second,
	}
	for _, m := range mutate {
		m(&cfg)
	}
	r := &rig{ch: newFakeCH(), st: newFakeStream(), wm: newFakeWatermarks(cfg.LeaseTTL)}
	if err := r.wm.Init(context.Background(), cfg.PartitionCount, 0); err != nil {
		t.Fatal(err)
	}
	r.p = New(cfg, r.ch, r.ch, r.st, r.wm)
	return r
}

func (r *rig) step(t *testing.T) Result {
	t.Helper()
	res, err := r.p.Step(context.Background(), 0)
	if err != nil {
		t.Fatalf("Step: %v", err)
	}
	return res
}

func rootSpan(trace string) Span {
	return Span{TraceID: trace, SpanID: trace + "-r", Service: "agent", StatusCode: "Ok", StartMicros: 1_000_000, DurationMs: 10}
}

func childSpan(trace, id string) Span {
	return Span{TraceID: trace, SpanID: id, ParentSpanID: trace + "-r", Service: "llm", StatusCode: "Ok", StartMicros: 1_100_000, DurationMs: 1}
}

func TestAnArrivalStampedExactlyAtTheWindowEdgeIsSeenOnce(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 60*second, true, rootSpan("a1")) // exactly w1 of the first window
	r.ch.nowMs = 70*second + grace

	first := r.step(t) // (0, 60 s]
	if first.W0 != 0 || first.W1 != 60*second || first.Assembled != 1 {
		t.Fatalf("first window %+v", first)
	}
	next := r.step(t) // (60 s, 70 s]
	if next.W0 != 60*second || next.W1 != 70*second || next.Assembled != 0 || next.Reassembled != 0 {
		t.Fatalf("second window %+v", next)
	}
	if len(r.st.stored) != 1 {
		t.Fatalf("events = %d, want 1", len(r.st.stored))
	}
}

func TestTheWindowIsCappedAndNeverPastClickHousesNowMinusGrace(t *testing.T) {
	r := newRig(t)
	r.ch.nowMs = 200*second + grace
	res := r.step(t)
	if res.W1 != 60*second || res.CaughtUp {
		t.Fatalf("a backlog is worked off in steps: %+v", res)
	}
	r.ch.nowMs = 30*second + grace
	r.wm.rows[[2]int{1, 0}].wm = 30 * second
	res = r.step(t)
	if !res.CaughtUp || res.Scanned {
		t.Fatalf("nothing is past the watermark yet: %+v", res)
	}
}

func TestACrashBetweenTheSummaryAndTheWatermarkResumesWithNoGap(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"), childSpan("a1", "c1"))
	r.ch.arrive("a2", 20*second, true, rootSpan("a2"))
	r.ch.nowMs = 60*second + grace

	r.wm.failAdvance = errBoom
	if _, err := r.p.Step(context.Background(), 0); !errors.Is(err, errBoom) {
		t.Fatalf("want the advance error, got %v", err)
	}
	if got := r.wm.watermark(1, 0); got != 0 {
		t.Fatalf("watermark moved to %d without the commit", got)
	}
	if len(r.ch.summaries) != 2 || len(r.st.stored) != 2 {
		t.Fatalf("outputs of the failed window: %d summaries, %d events", len(r.ch.summaries), len(r.st.stored))
	}

	res := r.step(t) // resumes from w0 = 0
	if res.W0 != 0 || res.W1 != 60*second {
		t.Fatalf("resumed from the wrong place: %+v", res)
	}
	if got := r.wm.watermark(1, 0); got != 60*second {
		t.Fatalf("watermark = %d", got)
	}
	if len(r.st.stored) != 2 || r.st.calls != 4 {
		t.Fatalf("stored %d events after %d publishes: the repeat must be deduplicated", len(r.st.stored), r.st.calls)
	}
	if r.ch.summaries["a1/1.0.0"].SpanCount != 2 {
		t.Fatalf("summary %+v", r.ch.summaries["a1/1.0.0"])
	}
}

func TestAFailedPublishLeavesTheWindowOpenAndLosesNoEvent(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.arrive("a2", 20*second, true, rootSpan("a2"))
	r.ch.nowMs = 60*second + grace
	r.st.failAt, r.st.failErr = 2, errBoom

	if _, err := r.p.Step(context.Background(), 0); !errors.Is(err, errBoom) {
		t.Fatalf("want the publish error, got %v", err)
	}
	if got := r.wm.watermark(1, 0); got != 0 {
		t.Fatalf("watermark = %d, want 0", got)
	}
	r.step(t)
	if len(r.st.stored) != 2 {
		t.Fatalf("events = %v", r.st.stored)
	}
}

func TestALateSpanReassemblesTheTraceWithAHigherRevision(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 60*second + grace
	r.step(t)

	r.ch.arrive("a1", 80*second, false, childSpan("a1", "late"))
	r.ch.nowMs = 90*second + grace
	res := r.step(t) // (60 s, 90 s]: no root in the window, already assembled
	if res.Reassembled != 1 || res.Assembled != 0 {
		t.Fatalf("result %+v", res)
	}
	got := r.ch.summaries["a1/1.0.0"]
	if got.RevisionMs != 80*second || got.SpanCount != 2 {
		t.Fatalf("summary %+v", got)
	}
	if len(r.st.stored) != 2 || r.st.stored[0].Revision != 10*second || r.st.stored[1].Revision != 80*second {
		t.Fatalf("events %+v", r.st.stored)
	}
	if r.st.stored[0].MsgID() == r.st.stored[1].MsgID() {
		t.Fatal("a new revision is a new message")
	}
}

func TestAnIdenticalReassemblyIsTheSameRowAndADeduplicatedPublish(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"), childSpan("a1", "c1"))
	r.ch.nowMs = 60*second + grace
	r.step(t)
	before := r.ch.summaries["a1/1.0.0"]

	r.wm.rows[[2]int{1, 0}].wm = 0 // replay the same window
	r.step(t)
	if got := r.ch.summaries["a1/1.0.0"]; got != before || len(r.ch.summaries) != 1 {
		t.Fatalf("row changed: %+v -> %+v", before, got)
	}
	if len(r.st.stored) != 1 || r.st.calls != 2 {
		t.Fatalf("stored %d after %d publishes", len(r.st.stored), r.st.calls)
	}
}

func TestANonRootArrivalOfAnUnassembledTraceWaitsForItsRoot(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, false, childSpan("a1", "c1"))
	r.ch.nowMs = 60*second + grace
	res := r.step(t)
	if res.Waiting != 1 || res.Assembled != 0 || len(r.st.stored) != 0 {
		t.Fatalf("result %+v, events %v", res, r.st.stored)
	}

	r.ch.arrive("a1", 80*second, true, rootSpan("a1"))
	r.ch.nowMs = 90*second + grace
	r.step(t)
	got := r.ch.summaries["a1/1.0.0"]
	if got.SpanCount != 2 || got.RevisionMs != 80*second {
		t.Fatalf("the root's arrival reads every span of the trace: %+v", got)
	}
}

func TestATraceWhoseRootIsMissingFromSpansIsSkippedAndCountedNotStuck(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true) // arrival says root, spans has none
	r.ch.arrive("a2", 20*second, true, rootSpan("a2"))
	r.ch.nowMs = 60*second + grace
	res := r.step(t)
	if res.Skipped != 1 || res.Assembled != 1 {
		t.Fatalf("result %+v", res)
	}
	if got := r.wm.watermark(1, 0); got != 60*second {
		t.Fatalf("watermark = %d: a bad trace must not stall the partition", got)
	}
}

func TestSpansAreReadInBatchesWithTheLookbackBound(t *testing.T) {
	r := newRig(t, func(c *Config) { c.BatchSize = 2; c.Lookback = 10 * time.Second })
	for _, id := range []string{"a1", "a2", "a3", "a4", "a5"} {
		r.ch.arrive(id, 10*second, true, rootSpan(id))
	}
	r.ch.nowMs = 60*second + grace
	r.step(t)
	if len(r.ch.spanReads) != 3 {
		t.Fatalf("span reads = %v", r.ch.spanReads)
	}
	for _, ids := range r.ch.spanReads {
		if len(ids) > 2 {
			t.Fatalf("a batch of %d", len(ids))
		}
	}
	if r.ch.sinces[0] != 60*second-10*second {
		t.Fatalf("since = %d", r.ch.sinces[0])
	}
}

func TestTheScanCarriesThePartitionCountAndPartition(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PartitionCount = 4 })
	r.ch.nowMs = 60*second + grace
	if _, err := r.p.Step(context.Background(), 3); err != nil {
		t.Fatal(err)
	}
	if got := r.ch.scans[0]; got.count != 4 || got.partition != 3 {
		t.Fatalf("scan %+v", got)
	}
}

func TestOnlyTheOwnersOfAPartitionAssembleItAndAStaleOwnerIsFenced(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 60*second + grace

	other := New(r.p.cfg, r.ch, r.ch, r.st, r.wm)
	other.cfg.Owner = "b"

	// a holds the lease: b gets nothing to do.
	if _, err := r.p.Step(context.Background(), 0); err != nil {
		t.Fatal(err)
	}
	r.wm.rows[[2]int{1, 0}].wm = 0 // pretend a's advance never happened, to give b a window
	res, err := other.Step(context.Background(), 0)
	if err != nil || res.Owned {
		t.Fatalf("b must not own a live lease: %+v, %v", res, err)
	}

	// a stalls past its lease; b takes over and a, resuming, is fenced.
	stale, ok, _ := r.wm.Claim(context.Background(), 1, 0, "a")
	if !ok {
		t.Fatal("a renews its own lease")
	}
	r.wm.advanceClock(2 * r.p.cfg.LeaseTTL)
	res, err = other.Step(context.Background(), 0)
	if err != nil || !res.Owned {
		t.Fatalf("b takes the expired partition: %+v, %v", res, err)
	}
	okAdv, _ := r.wm.Advance(context.Background(), 1, stale, "a", 60*second)
	if okAdv {
		t.Fatal("the stale owner advanced the watermark")
	}
	if len(r.st.stored) != 1 {
		t.Fatalf("two owners must not double the output: %v", r.st.stored)
	}
}

func TestAStepThatLosesItsLeaseReportsErrLeaseLost(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 60*second + grace
	r.p.afterWrite = func() {
		r.wm.advanceClock(2 * r.p.cfg.LeaseTTL)
		if _, ok, _ := r.wm.Claim(context.Background(), 1, 0, "b"); !ok {
			t.Fatal("b should take the expired lease")
		}
	}
	_, err := r.p.Step(context.Background(), 0)
	if !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("want ErrLeaseLost, got %v", err)
	}
}

func TestInitStartsANewPartitionCountAtTheLookbackBound(t *testing.T) {
	r := newRig(t)
	r.ch.nowMs = 10 * 24 * 3600 * second
	if err := r.p.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	// the rig already holds count 1 at 0: Start never lowers or raises existing rows
	if got := r.wm.watermark(1, 0); got != 0 {
		t.Fatalf("existing row changed to %d", got)
	}
	r2 := newRig(t, func(c *Config) { c.PartitionCount = 2 })
	delete(r2.wm.rows, [2]int{2, 0})
	delete(r2.wm.rows, [2]int{2, 1})
	r2.ch.nowMs = 10 * 24 * 3600 * second
	if err := r2.p.Start(context.Background()); err != nil {
		t.Fatal(err)
	}
	want := r2.ch.nowMs - grace - int64(48*time.Hour/time.Millisecond)
	if got := r2.wm.watermark(2, 1); got != want {
		t.Fatalf("initial watermark = %d, want %d", got, want)
	}
}

type recordingMetrics struct {
	mu    sync.Mutex
	steps []Result
	errs  []error
}

func (m *recordingMetrics) Step(_ int, r Result, _ time.Duration, err error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.steps = append(m.steps, r)
	m.errs = append(m.errs, err)
}

func (m *recordingMetrics) count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.steps)
}

func TestRunWorksOffABacklogWithoutSleepingThenStopsWithTheContext(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PollInterval = time.Hour })
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 150*second + grace // three windows of 60 s behind
	m := &recordingMetrics{}
	r.p.WithMetrics(m).WithLogger(slog.New(slog.NewJSONHandler(io.Discard, nil)))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.p.Run(ctx) }()

	deadline := time.After(5 * time.Second)
	for r.wm.watermark(1, 0) != 150*second {
		select {
		case <-deadline:
			t.Fatalf("watermark stuck at %d after %d steps", r.wm.watermark(1, 0), m.count())
		case <-time.After(time.Millisecond):
		}
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run returned %v", err)
	}
	if len(r.st.stored) != 1 {
		t.Fatalf("events = %v", r.st.stored)
	}
}

func TestRunLogsAFailedStepAndCarriesOn(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PollInterval = time.Millisecond })
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 60*second + grace
	r.st.failAt, r.st.failErr = 1, errBoom
	m := &recordingMetrics{}
	var logs bytes.Buffer
	r.p.WithMetrics(m).WithLogger(slog.New(slog.NewJSONHandler(&logs, nil)))

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.p.Run(ctx) }()
	deadline := time.After(5 * time.Second)
	for r.wm.watermark(1, 0) != 60*second {
		select {
		case <-deadline:
			t.Fatal("the retry never advanced the watermark")
		case <-time.After(time.Millisecond):
		}
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run returned %v", err)
	}
	if !bytes.Contains(logs.Bytes(), []byte(`"msg":"step failed"`)) {
		t.Fatalf("log: %s", logs.String())
	}
	if bytes.Contains(logs.Bytes(), []byte("a1-r")) {
		t.Fatal("span ids must not be logged")
	}
}
