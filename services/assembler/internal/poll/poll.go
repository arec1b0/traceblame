package poll

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"time"
)

// Config holds the thresholds of ADR-0003; every value comes from the service's config.
type Config struct {
	Owner            string        // this replica's identity in the lease
	PartitionCount   int           // N: a trace belongs to cityHash64(trace_id) % N
	Grace            time.Duration // batching margin behind ClickHouse's clock
	MaxWindow        time.Duration // longest window of one step
	LeaseTTL         time.Duration
	BatchSize        int           // traces per spans read
	Lookback         time.Duration // how far back a trace's spans are read
	InitialLookback  time.Duration // where a partition count with no earlier state starts
	PollInterval     time.Duration
	AssemblerVersion string
}

// Result describes one step, for the loop's metrics and the tests.
type Result struct {
	Owned        bool  // false: another replica holds the partition's lease
	UpperMs      int64 // ClickHouse's now - grace when the step ran
	W0, W1       int64
	Scanned      bool
	CaughtUp     bool // W1 reached ClickHouse's now - grace
	Arrivals     int
	Assembled    int // first assembly of a trace
	Reassembled  int // the trace had a summary before
	Waiting      int // spans without a root, trace never assembled
	Skipped      int // a root arrived but no root span could be read
	LateDelaysMs []int64
}

// Metrics receives each step's outcome; the zero value of the loop uses a no-op.
type Metrics interface {
	Step(partition int, r Result, took time.Duration, err error)
	Round(r RoundResult)
}

type nopMetrics struct{}

func (nopMetrics) Step(int, Result, time.Duration, error) {}
func (nopMetrics) Round(RoundResult)                      {}

// Poller is the trace-assembler's loop.
type Poller struct {
	cfg     Config
	src     Source
	sum     Summaries
	pub     Publisher
	wm      Watermarks
	metrics Metrics
	log     *slog.Logger

	// afterWrite runs between the outputs and the watermark advance; tests use it to
	// interleave another replica there.
	afterWrite func()

	// held is the partitions this replica owns, with their leases. Only Round and Step touch
	// it, and Run calls them from one goroutine.
	held map[int]Lease
}

// RoundResult is what one pass over the partitions did.
type RoundResult struct {
	Live     int // replicas alive, this one included
	Target   int // fair share: ceil(partitions / live)
	Released []int
	Results  map[int]Result
	Errors   map[int]error
}

// New wires a poller. Metrics and the logger default to no-ops.
func New(cfg Config, src Source, sum Summaries, pub Publisher, wm Watermarks) *Poller {
	return &Poller{
		cfg: cfg, src: src, sum: sum, pub: pub, wm: wm,
		metrics: nopMetrics{}, log: slog.New(slog.DiscardHandler), held: map[int]Lease{},
	}
}

// WithMetrics sets the metrics receiver.
func (p *Poller) WithMetrics(m Metrics) *Poller { p.metrics = m; return p }

// WithLogger sets the logger (JSON, never content).
func (p *Poller) WithLogger(l *slog.Logger) *Poller { p.log = l; return p }

// Start creates the partitions of the configured count that do not exist yet. A new count
// starts at the smallest watermark of the most recent other count (Watermarks.Init), else
// at ClickHouse's now - grace - initial lookback (the 2-day TTL of trace_arrivals by default).
func (p *Poller) Start(ctx context.Context) error {
	upper, err := p.src.Upper(ctx, p.cfg.Grace.Milliseconds())
	if err != nil {
		return fmt.Errorf("poll: clickhouse clock: %w", err)
	}
	return p.wm.Init(ctx, p.cfg.PartitionCount, upper-p.cfg.InitialLookback.Milliseconds())
}

// Step does one window of one partition. The order is the claim boundary of ADR-0003:
//
//  1. scan (w0, w1] and read the spans of the traces to (re)assemble;
//  2. write trace_summary (idempotent: same key and revision, same row);
//  3. publish trace.completed with publish-ack and Nats-Msg-Id (idempotent in the duplicate window);
//  4. advance the watermark, fenced by the lease epoch: the commit point.
func (p *Poller) Step(ctx context.Context, partition int) (Result, error) {
	lease, ok, err := p.wm.Claim(ctx, p.cfg.PartitionCount, partition, p.cfg.Owner)
	if err != nil {
		return Result{}, fmt.Errorf("poll: claim partition %d: %w", partition, err)
	}
	if !ok {
		delete(p.held, partition)
		return Result{}, nil
	}
	p.held[partition] = lease
	res := Result{Owned: true, W0: lease.WatermarkMs, W1: lease.WatermarkMs}

	upper, err := p.src.Upper(ctx, p.cfg.Grace.Milliseconds())
	if err != nil {
		return res, fmt.Errorf("poll: clickhouse clock: %w", err)
	}
	res.UpperMs = upper
	if upper <= res.W0 {
		res.CaughtUp = true
		return res, nil
	}
	res.W1 = min(upper, res.W0+p.cfg.MaxWindow.Milliseconds())
	res.CaughtUp = res.W1 == upper

	arrivals, err := p.src.Scan(ctx, p.cfg.PartitionCount, partition, res.W0, res.W1)
	if err != nil {
		return res, fmt.Errorf("poll: scan: %w", err)
	}
	res.Scanned, res.Arrivals = true, len(arrivals)

	todo, err := p.classify(ctx, arrivals, &res)
	if err != nil {
		return res, err
	}
	if err := p.assemble(ctx, todo, res.W1, &res); err != nil {
		return res, err
	}

	if p.afterWrite != nil {
		p.afterWrite()
	}
	advanced, err := p.wm.Advance(ctx, p.cfg.PartitionCount, lease, p.cfg.Owner, res.W1)
	if err != nil {
		return res, fmt.Errorf("poll: advance partition %d: %w", partition, err)
	}
	if !advanced {
		delete(p.held, partition)
		return res, ErrLeaseLost
	}
	lease.WatermarkMs = res.W1
	p.held[partition] = lease
	return res, nil
}

// Round is one pass over the partitions with a fair share (ADR-0003 amendment). The replica
// registers its heartbeat and learns how many replicas are alive; its share is
// ceil(partitions / live). It gives up what it holds above the share, between two steps, when
// no window of its own is in flight, and it claims a partition only while it holds fewer than
// its share. A joining replica therefore takes the partitions the incumbent releases, and the
// survivors take a dead replica's once its lease and heartbeat expire.
func (p *Poller) Round(ctx context.Context) (RoundResult, error) {
	live, err := p.wm.Heartbeat(ctx, p.cfg.Owner)
	if err != nil {
		return RoundResult{}, fmt.Errorf("poll: heartbeat: %w", err)
	}
	live = max(live, 1)
	out := RoundResult{
		Live: live, Target: (p.cfg.PartitionCount + live - 1) / live,
		Results: map[int]Result{}, Errors: map[int]error{},
	}

	for _, part := range p.excess(out.Target) {
		if err := p.wm.Release(ctx, p.cfg.PartitionCount, p.held[part], p.cfg.Owner); err != nil {
			out.Errors[part] = fmt.Errorf("poll: release partition %d: %w", part, err)
			continue
		}
		delete(p.held, part)
		out.Released = append(out.Released, part)
	}

	for part := range p.cfg.PartitionCount {
		if _, mine := p.held[part]; !mine && len(p.held) >= out.Target {
			continue
		}
		t0 := time.Now()
		res, err := p.Step(ctx, part)
		p.metrics.Step(part, res, time.Since(t0), err)
		out.Results[part] = res
		if err != nil {
			out.Errors[part] = err
		}
	}
	p.metrics.Round(out)
	return out, nil
}

// excess is the held partitions above the share, highest numbers first.
func (p *Poller) excess(target int) []int {
	if len(p.held) <= target {
		return nil
	}
	parts := make([]int, 0, len(p.held))
	for part := range p.held {
		parts = append(parts, part)
	}
	sort.Sort(sort.Reverse(sort.IntSlice(parts)))
	return parts[:len(p.held)-target]
}

// classify keeps the arrivals to (re)assemble: a trace that has a summary (late spans, or a
// replayed window), and a trace whose root arrived. A trace with spans but no root that was
// never assembled waits for its root.
func (p *Poller) classify(ctx context.Context, arrivals []Arrival, res *Result) ([]job, error) {
	if len(arrivals) == 0 {
		return nil, nil
	}
	ids := make([]string, len(arrivals))
	for i, a := range arrivals {
		ids[i] = a.TraceID
	}
	before, err := p.src.Assembled(ctx, ids)
	if err != nil {
		return nil, fmt.Errorf("poll: assembled check: %w", err)
	}
	var todo []job
	for _, a := range arrivals {
		prev, had := before[a.TraceID]
		switch {
		case had:
			todo = append(todo, job{Arrival: a, again: true, prevRevisionMs: prev})
		case a.HasRoot:
			todo = append(todo, job{Arrival: a})
		default:
			res.Waiting++
		}
	}
	sort.Slice(todo, func(i, j int) bool { return todo[i].TraceID < todo[j].TraceID })
	return todo, nil
}

// job is an arrival to (re)assemble; again marks a trace that already has a summary.
type job struct {
	Arrival
	again          bool
	prevRevisionMs int64
}

func (p *Poller) assemble(ctx context.Context, todo []job, w1 int64, res *Result) error {
	batch := max(p.cfg.BatchSize, 1)
	since := w1 - p.cfg.Lookback.Milliseconds()
	for start := 0; start < len(todo); start += batch {
		chunk := todo[start:min(start+batch, len(todo))]
		ids := make([]string, len(chunk))
		for i, j := range chunk {
			ids[i] = j.TraceID
		}
		spans, err := p.src.Spans(ctx, ids, since)
		if err != nil {
			return fmt.Errorf("poll: read spans: %w", err)
		}
		byTrace := map[string][]Span{}
		for _, s := range spans {
			byTrace[s.TraceID] = append(byTrace[s.TraceID], s)
		}
		var rows []Summary
		for _, j := range chunk {
			row, ok := Assemble(p.cfg.AssemblerVersion, j.LastArrivalMs, byTrace[j.TraceID])
			if !ok {
				res.Skipped++
				p.log.Warn("no root span to summarise", "trace_id", j.TraceID, "component", "assembler",
					"version", p.cfg.AssemblerVersion)
				continue
			}
			rows = append(rows, row)
			if !j.again {
				res.Assembled++
				continue
			}
			res.Reassembled++
			if d := j.LastArrivalMs - j.prevRevisionMs; d > 0 {
				res.LateDelaysMs = append(res.LateDelaysMs, d)
			}
		}
		if len(rows) == 0 {
			continue
		}
		if err := p.sum.Write(ctx, rows); err != nil {
			return fmt.Errorf("poll: write trace_summary: %w", err)
		}
		for _, r := range rows {
			e := Event{TraceID: r.TraceID, Revision: r.RevisionMs, SpanCount: r.SpanCount, AssemblerVersion: r.AssemblerVersion}
			if err := p.pub.Publish(ctx, e); err != nil {
				return fmt.Errorf("poll: publish %s: %w", e.MsgID(), err)
			}
		}
	}
	return nil
}

// Run loops until ctx ends. A replica that is behind steps again at once; one that has caught
// up sleeps for the poll interval.
func (p *Poller) Run(ctx context.Context) error {
	if err := p.Start(ctx); err != nil {
		return err
	}
	for ctx.Err() == nil {
		behind := false
		round, err := p.Round(ctx)
		switch {
		case ctx.Err() != nil:
			return nil
		case err != nil:
			p.log.Error("round failed", "error", err.Error(), "component", "assembler", "version", p.cfg.AssemblerVersion)
		default:
			for part, res := range round.Results {
				stepErr := round.Errors[part]
				switch {
				case stepErr == nil:
					behind = behind || (res.Owned && !res.CaughtUp)
				case errors.Is(stepErr, ErrLeaseLost):
					p.log.Info("lease lost", "partition", part, "component", "assembler", "version", p.cfg.AssemblerVersion)
				case ctx.Err() == nil:
					p.log.Error("step failed", "partition", part, "error", stepErr.Error(), "component", "assembler", "version", p.cfg.AssemblerVersion)
				}
			}
			for _, part := range round.Released {
				p.log.Info("partition released for a fair share", "partition", part, "live", round.Live,
					"target", round.Target, "component", "assembler", "version", p.cfg.AssemblerVersion)
			}
		}
		if behind {
			continue
		}
		select {
		case <-ctx.Done():
		case <-time.After(p.cfg.PollInterval):
		}
	}
	return nil
}
