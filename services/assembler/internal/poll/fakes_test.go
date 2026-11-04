package poll

import (
	"context"
	"errors"
	"sort"
	"sync"
	"time"
)

// fakeCH is ClickHouse: the arrivals table, the spans table and trace_summary, with the
// semantics of the SQL (window (w0, w1], grouped by trace, partition filter).
type fakeCH struct {
	mu        sync.Mutex
	nowMs     int64
	arrivals  []fakeArrival
	spans     map[string][]Span
	summaries map[string]Summary // key trace/version
	partOf    func(trace string, count int) int
	scans     []scanCall
	spanReads [][]string
	sinces    []int64
}

type fakeArrival struct {
	trace string
	atMs  int64
	root  bool
}

type scanCall struct {
	count, partition int
	w0, w1           int64
}

func newFakeCH() *fakeCH {
	return &fakeCH{
		spans:     map[string][]Span{},
		summaries: map[string]Summary{},
		partOf:    func(trace string, count int) int { return int(trace[0]) % count },
	}
}

func (f *fakeCH) arrive(trace string, atMs int64, root bool, spans ...Span) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.arrivals = append(f.arrivals, fakeArrival{trace, atMs, root})
	f.spans[trace] = append(f.spans[trace], spans...)
}

func (f *fakeCH) Upper(_ context.Context, graceMs int64) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.nowMs - graceMs, nil
}

func (f *fakeCH) Scan(_ context.Context, count, partition int, w0, w1 int64) ([]Arrival, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.scans = append(f.scans, scanCall{count, partition, w0, w1})
	by := map[string]*Arrival{}
	for _, a := range f.arrivals {
		if a.atMs <= w0 || a.atMs > w1 || f.partOf(a.trace, count) != partition {
			continue
		}
		cur := by[a.trace]
		if cur == nil {
			cur = &Arrival{TraceID: a.trace}
			by[a.trace] = cur
		}
		cur.HasRoot = cur.HasRoot || a.root
		cur.LastArrivalMs = max(cur.LastArrivalMs, a.atMs)
	}
	out := make([]Arrival, 0, len(by))
	for _, a := range by {
		out = append(out, *a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].TraceID < out[j].TraceID })
	return out, nil
}

func (f *fakeCH) Assembled(_ context.Context, ids []string) (map[string]int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := map[string]int64{}
	for _, s := range f.summaries {
		for _, id := range ids {
			if s.TraceID == id {
				out[id] = max(out[id], s.RevisionMs)
			}
		}
	}
	return out, nil
}

func (f *fakeCH) Spans(_ context.Context, ids []string, sinceMs int64) ([]Span, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.spanReads = append(f.spanReads, append([]string(nil), ids...))
	f.sinces = append(f.sinces, sinceMs)
	var out []Span
	for _, id := range ids {
		out = append(out, f.spans[id]...)
	}
	return out, nil
}

// Write is trace_summary: a ReplacingMergeTree keyed by (trace, version), the higher revision wins.
func (f *fakeCH) Write(_ context.Context, rows []Summary) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, r := range rows {
		key := r.TraceID + "/" + r.AssemblerVersion
		if cur, ok := f.summaries[key]; ok && cur.RevisionMs > r.RevisionMs {
			continue
		}
		f.summaries[key] = r
	}
	return nil
}

// fakeStream is JetStream with a duplicate window that never expires inside a test.
type fakeStream struct {
	mu      sync.Mutex
	seen    map[string]bool
	stored  []Event
	calls   int
	failAt  int // fail the n-th publish call (1-based), once
	failErr error
}

func newFakeStream() *fakeStream { return &fakeStream{seen: map[string]bool{}} }

func (s *fakeStream) Publish(_ context.Context, e Event) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	if s.failAt == s.calls {
		return s.failErr
	}
	if s.seen[e.MsgID()] {
		return nil // acked as a duplicate
	}
	s.seen[e.MsgID()] = true
	s.stored = append(s.stored, e)
	return nil
}

// fakeWatermarks models assembler_partitions: leases with an epoch, a fenced advance.
type fakeWatermarks struct {
	mu          sync.Mutex
	now         time.Time
	ttl         time.Duration
	rows        map[[2]int]*wmRow
	replicas    map[string]time.Time // owner -> last heartbeat
	failAdvance error                // returned once by Advance
}

type wmRow struct {
	wm         int64
	owner      string
	epoch      int64
	leaseUntil time.Time
}

func newFakeWatermarks(ttl time.Duration) *fakeWatermarks {
	return &fakeWatermarks{
		now: time.Unix(1_000, 0), ttl: ttl, rows: map[[2]int]*wmRow{}, replicas: map[string]time.Time{},
	}
}

func (w *fakeWatermarks) Init(_ context.Context, count int, initialMs int64) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	for p := range count {
		if _, ok := w.rows[[2]int{count, p}]; !ok {
			w.rows[[2]int{count, p}] = &wmRow{wm: initialMs}
		}
	}
	return nil
}

func (w *fakeWatermarks) Claim(_ context.Context, count, partition int, owner string) (Lease, bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	r := w.rows[[2]int{count, partition}]
	held := r.owner != "" && w.now.Before(r.leaseUntil)
	if held && r.owner != owner {
		return Lease{}, false, nil
	}
	if !held || r.owner != owner {
		r.epoch++
	}
	r.owner, r.leaseUntil = owner, w.now.Add(w.ttl)
	return Lease{Partition: partition, Epoch: r.epoch, WatermarkMs: r.wm}, true, nil
}

func (w *fakeWatermarks) Advance(_ context.Context, count int, l Lease, owner string, newMs int64) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if err := w.failAdvance; err != nil {
		w.failAdvance = nil
		return false, err
	}
	r := w.rows[[2]int{count, l.Partition}]
	if r.owner != owner || r.epoch != l.Epoch || r.wm != l.WatermarkMs {
		return false, nil
	}
	r.wm = newMs
	r.leaseUntil = w.now.Add(w.ttl)
	return true, nil
}

// Heartbeat registers the replica and counts those seen within the lease TTL.
func (w *fakeWatermarks) Heartbeat(_ context.Context, owner string) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.replicas[owner] = w.now
	live := 0
	for _, at := range w.replicas {
		if w.now.Sub(at) < w.ttl {
			live++
		}
	}
	return live, nil
}

// Release gives a partition up if the lease is still the owner's at that epoch.
func (w *fakeWatermarks) Release(_ context.Context, count int, l Lease, owner string) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if r := w.rows[[2]int{count, l.Partition}]; r.owner == owner && r.epoch == l.Epoch {
		r.owner, r.leaseUntil = "", time.Time{}
	}
	return nil
}

func (w *fakeWatermarks) owner(count, partition int) string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.rows[[2]int{count, partition}].owner
}

func (w *fakeWatermarks) epoch(count, partition int) int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.rows[[2]int{count, partition}].epoch
}

func (w *fakeWatermarks) watermark(count, partition int) int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.rows[[2]int{count, partition}].wm
}

func (w *fakeWatermarks) advanceClock(d time.Duration) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.now = w.now.Add(d)
}

var errBoom = errors.New("boom")
