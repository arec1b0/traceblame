package poll

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"reflect"
	"sort"
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
		AssemblerVersion: "1.0.0", PollInterval: 2 * time.Second, InitialLookback: 36 * time.Hour,
		MinWindow: time.Second, WindowRecoverySteps: 5,
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

	res := r.step(t) // resumes from w0 = 0, with the window halved by the failed step
	if res.W0 != 0 || res.W1 != 30*second {
		t.Fatalf("resumed from the wrong place: %+v", res)
	}
	r.step(t)
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

func TestInitStartsANewPartitionCountAtTheInitialLookbackNotTheSpansLookback(t *testing.T) {
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
	want := r2.ch.nowMs - grace - int64(36*time.Hour/time.Millisecond)
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

func (m *recordingMetrics) Round(RoundResult) {}

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

// --- fair share of partitions (review: replicas that join later must get work) ---------------

func (r *rig) second(t *testing.T, owner string) *Poller {
	t.Helper()
	p := New(r.p.cfg, r.ch, r.ch, r.st, r.wm)
	p.cfg.Owner = owner
	return p
}

func traceNames(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = string(rune('a'+i%26)) + fmt.Sprintf("%031d", i) // spread over the partitions
	}
	return out
}

func round(t *testing.T, p *Poller) RoundResult {
	t.Helper()
	res, err := p.Round(context.Background())
	if err != nil {
		t.Fatalf("Round: %v", err)
	}
	return res
}

func heldBy(p *Poller) []int {
	var out []int
	for part := range p.held {
		out = append(out, part)
	}
	sort.Ints(out)
	return out
}

func TestAReplicaThatJoinsLaterGetsItsFairShareAndNothingIsAssembledTwice(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PartitionCount = 4 })
	a, b := r.p, r.second(t, "b")

	first := traceNames(24)
	for _, id := range first {
		r.ch.arrive(id, 10*second, true, rootSpan(id))
	}
	r.ch.nowMs = 60*second + grace
	res := round(t, a)
	if got := heldBy(a); len(got) != 4 || res.Live != 1 || res.Target != 4 {
		t.Fatalf("alone, a owns everything: held %v, %+v", got, res)
	}

	later := traceNames(48)[24:]
	for _, id := range later {
		r.ch.arrive(id, 70*second, true, rootSpan(id))
	}
	r.ch.nowMs = 120*second + grace

	// b joins: it registers and finds every partition leased by a.
	if res := round(t, b); len(heldBy(b)) != 0 || res.Live != 2 || res.Target != 2 {
		t.Fatalf("b cannot take a live lease: held %v, %+v", heldBy(b), res)
	}
	// a sees two live replicas, keeps its share and gives the rest up after its steps.
	resA := round(t, a)
	if got := heldBy(a); len(got) != 2 || len(resA.Released) != 2 {
		t.Fatalf("a keeps half: held %v, released %v", got, resA.Released)
	}
	round(t, b)
	if got := heldBy(b); len(got) != 2 {
		t.Fatalf("b takes the released half: held %v", got)
	}
	union := map[int]bool{}
	for _, part := range append(heldBy(a), heldBy(b)...) {
		union[part] = true
	}
	if len(union) != 4 {
		t.Fatalf("a %v and b %v must cover the four partitions without overlap", heldBy(a), heldBy(b))
	}

	round(t, a)
	round(t, b)
	want := len(first) + len(later)
	if len(r.st.stored) != want || r.st.calls != want {
		t.Fatalf("%d events stored after %d publishes, want %d each: a trace was assembled twice",
			len(r.st.stored), r.st.calls, want)
	}
	for part := range 4 {
		if r.wm.watermark(4, part) < 120*second {
			t.Fatalf("partition %d stopped at %d", part, r.wm.watermark(4, part))
		}
	}
}

func TestAnEvenShareDoesNotChurn(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PartitionCount = 2 })
	a, b, c := r.p, r.second(t, "b"), r.second(t, "c")
	r.ch.nowMs = 60*second + grace
	for range 6 { // three replicas, two partitions: one each for a and b, none for c
		round(t, a)
		round(t, b)
		round(t, c)
	}
	if len(heldBy(a))+len(heldBy(b))+len(heldBy(c)) != 2 || len(heldBy(c)) > 1 {
		t.Fatalf("a %v b %v c %v", heldBy(a), heldBy(b), heldBy(c))
	}
	epochs := [2]int64{r.wm.epoch(2, 0), r.wm.epoch(2, 1)}
	for range 6 {
		round(t, a)
		round(t, b)
		round(t, c)
	}
	if got := [2]int64{r.wm.epoch(2, 0), r.wm.epoch(2, 1)}; got != epochs {
		t.Fatalf("leases changed hands in steady state: epochs %v -> %v", epochs, got)
	}
}

func TestTheSurvivorTakesEveryPartitionOnceTheDeadReplicasLeaseExpires(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PartitionCount = 4 })
	a, b := r.p, r.second(t, "b")
	r.ch.nowMs = 60*second + grace
	for range 3 {
		round(t, b)
		round(t, a)
	}
	if len(heldBy(a)) != 2 || len(heldBy(b)) != 2 {
		t.Fatalf("a %v b %v", heldBy(a), heldBy(b))
	}
	// a stops (no more heartbeats); its leases and its heartbeat run out.
	r.wm.advanceClock(2 * r.p.cfg.LeaseTTL)
	res := round(t, b)
	if res.Live != 1 || len(heldBy(b)) != 4 {
		t.Fatalf("b must own all four: held %v, %+v", heldBy(b), res)
	}
}

func TestAReleasedPartitionKeepsItsWatermarkAndTheNextOwnerResumesThere(t *testing.T) {
	r := newRig(t, func(c *Config) { c.PartitionCount = 2 })
	a, b := r.p, r.second(t, "b")
	r.ch.arrive("a"+fmt.Sprintf("%031d", 1), 10*second, true, rootSpan("a"+fmt.Sprintf("%031d", 1)))
	r.ch.nowMs = 60*second + grace
	round(t, a)
	atA := [2]int64{r.wm.watermark(2, 0), r.wm.watermark(2, 1)}
	round(t, b)
	round(t, a) // releases one
	round(t, b) // takes it
	for part := range 2 {
		if r.wm.watermark(2, part) < atA[part] {
			t.Fatalf("partition %d went back from %d to %d", part, atA[part], r.wm.watermark(2, part))
		}
	}
	if len(r.st.stored) != 1 {
		t.Fatalf("events %d", len(r.st.stored))
	}
}

func TestAResultSaysWhichWatermarkIsCommitted(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 60*second + grace
	r.wm.failAdvance = errBoom
	res, err := r.p.Step(context.Background(), 0)
	if !errors.Is(err, errBoom) || res.CommittedMs != 0 {
		t.Fatalf("a window that did not commit leaves the watermark at its start: %+v, %v", res, err)
	}
	res = r.step(t) // the failed step halved the window
	if res.CommittedMs != 30*second || res.CommittedMs != res.W1 {
		t.Fatalf("a committed window moves it to W1: %+v", res)
	}
	r.ch.nowMs = 61*second + grace
	r.wm.rows[[2]int{1, 0}].wm = 40 * second
	if res = r.step(t); res.CommittedMs != 61*second {
		t.Fatalf("%+v", res)
	}
}

func TestACommittedWatermarkIsReportedEvenWhenTheClickHouseClockFails(t *testing.T) {
	r := newRig(t)
	r.wm.rows[[2]int{1, 0}].wm = 33 * second
	r.p.src = failingClock{r.ch}
	res, err := r.p.Step(context.Background(), 0)
	if err == nil || !res.Owned || res.CommittedMs != 33*second {
		t.Fatalf("%+v, %v", res, err)
	}
}

type failingClock struct{ *fakeCH }

func (failingClock) Upper(context.Context, int64) (int64, error) { return 0, errBoom }

// --- dense windows (review: an unbatched check and a window that never shrinks stall catch-up) ---

func TestTheAssembledCheckIsBatchedLikeTheSpansRead(t *testing.T) {
	r := newRig(t, func(c *Config) { c.BatchSize = 2 })
	for _, id := range traceNames(5) {
		r.ch.arrive(id, 10*second, true, rootSpan(id))
	}
	r.ch.nowMs = 60*second + grace
	r.step(t)
	if !reflect.DeepEqual(r.ch.assembledCalls, []int{2, 2, 1}) {
		t.Fatalf("Assembled was asked for %v ids per call, want batches of 2", r.ch.assembledCalls)
	}
	if len(r.st.stored) != 5 {
		t.Fatalf("events %d", len(r.st.stored))
	}
}

// denseRig: 600 traces over one minute, and a ClickHouse that cannot answer a window of more
// than 100 of them.
func denseRig(t *testing.T, mutate ...func(*Config)) *rig {
	t.Helper()
	r := newRig(t, append([]func(*Config){func(c *Config) {
		c.MinWindow, c.WindowRecoverySteps = 2*time.Second, 3
	}}, mutate...)...)
	for i, id := range traceNames(600) {
		r.ch.arrive(id, int64(i+1)*100, true, rootSpan(id)) // one every 100 ms, the first after w0
	}
	r.ch.nowMs = 60*second + grace
	r.ch.failScanAbove = 100
	return r
}

func TestAWindowThatErrorsIsHalvedUntilItFitsAndTheBacklogIsWorkedOff(t *testing.T) {
	r := denseRig(t)
	var failures, shrinks int
	var smallest int64 = 1 << 62
	for range 200 {
		res, err := r.p.Step(context.Background(), 0)
		if res.Shrank {
			shrinks++
		}
		if err != nil {
			failures++
		} else if res.WindowMs < smallest {
			smallest = res.WindowMs
		}
		if r.wm.watermark(1, 0) >= 60*second {
			break
		}
	}
	if got := r.wm.watermark(1, 0); got != 60*second {
		t.Fatalf("the watermark stopped at %d: a dense window stalled catch-up", got)
	}
	if failures == 0 || shrinks != failures {
		t.Fatalf("%d failures, %d shrinks: every failed step must shrink the window", failures, shrinks)
	}
	if smallest > 10*second {
		t.Fatalf("a window of 100 arrivals is 10 s here; the successful windows were %d ms", smallest)
	}
	if len(r.st.stored) != 600 || r.st.calls != 600 {
		t.Fatalf("%d events after %d publishes, want 600 each", len(r.st.stored), r.st.calls)
	}
}

func TestTheWindowNeverShrinksBelowItsFloorAndIsRecordedWhenItDoes(t *testing.T) {
	r := denseRig(t, func(c *Config) { c.MinWindow = 5 * time.Second })
	r.ch.failScanAbove = 10 // even the floor is too dense
	m := &recordingMetrics{}
	r.p.WithMetrics(m)
	var last Result
	shrinks := 0
	for range 10 {
		res, err := r.p.Step(context.Background(), 0)
		if err == nil {
			t.Fatal("a window of 50 arrivals cannot be answered here")
		}
		last = res
		if res.Shrank {
			shrinks++
		}
	}
	if last.WindowMs != 5_000 {
		t.Fatalf("window = %d ms, want the 5 s floor", last.WindowMs)
	}
	if shrinks != 4 { // 60 -> 30 -> 15 -> 7.5 -> 5 (floor)
		t.Fatalf("the window shrank %d times, want 4 (it stays at the floor afterwards)", shrinks)
	}
	if r.wm.watermark(1, 0) != 0 {
		t.Fatal("nothing committed")
	}
}

func TestAShrunkWindowGrowsBackAfterEnoughCleanSteps(t *testing.T) {
	r := denseRig(t)
	r.ch.failScanAbove = 0
	r.p.window[0] = 4 * time.Second // as if it had been shrunk
	r.ch.nowMs = 600*second + grace // plenty of backlog, all empty windows
	sizes := []int64{}
	for range 8 {
		res := r.step(t)
		sizes = append(sizes, res.WindowMs)
	}
	want := []int64{4_000, 4_000, 4_000, 8_000, 8_000, 8_000, 16_000, 16_000}
	if !reflect.DeepEqual(sizes, want) {
		t.Fatalf("windows %v, want %v: doubled after 3 clean steps", sizes, want)
	}
	for range 40 {
		r.step(t)
	}
	if got := r.p.window[0]; got != r.p.cfg.MaxWindow {
		t.Fatalf("the window stops growing at the maximum, got %s", got)
	}
}

func TestALostLeaseOrACancelledContextDoesNotShrinkTheWindow(t *testing.T) {
	r := newRig(t)
	r.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r.ch.nowMs = 60*second + grace
	r.p.afterWrite = func() {
		r.wm.advanceClock(2 * r.p.cfg.LeaseTTL)
		_, _, _ = r.wm.Claim(context.Background(), 1, 0, "b")
	}
	res, err := r.p.Step(context.Background(), 0)
	if !errors.Is(err, ErrLeaseLost) || res.Shrank {
		t.Fatalf("a fenced commit is not a reason to shrink: %+v, %v", res, err)
	}
	r2 := newRig(t)
	r2.ch.arrive("a1", 10*second, true, rootSpan("a1"))
	r2.ch.nowMs = 60*second + grace
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r2.st.failAt, r2.st.failErr = 1, context.Canceled
	res, _ = r2.p.Step(ctx, 0)
	if res.Shrank {
		t.Fatal("a cancelled context (shutdown) is not a reason to shrink")
	}
}

func TestAnUnsetFloorOrRecoveryCountCannotDriveTheWindowToZero(t *testing.T) {
	r := denseRig(t, func(c *Config) { c.MinWindow, c.WindowRecoverySteps = 0, 0 })
	r.ch.failScan = true // nothing can be answered: the window shrinks as far as it can
	for range 60 {
		if _, err := r.p.Step(context.Background(), 0); err == nil {
			t.Fatal("expected the scan to fail")
		}
	}
	if got := r.p.window[0]; got < time.Millisecond {
		t.Fatalf("the window fell to %s: a zero window never reaches a single arrival", got)
	}
	r.ch.failScan, r.ch.failScanAbove = false, 0
	steps := 0
	for r.wm.watermark(1, 0) < 60*second && steps < 100_000 {
		if _, err := r.p.Step(context.Background(), 0); err != nil {
			t.Fatal(err)
		}
		steps++
	}
	if r.wm.watermark(1, 0) < 60*second {
		t.Fatalf("the partition never caught up after %d steps", steps)
	}
}
