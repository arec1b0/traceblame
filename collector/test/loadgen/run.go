package main

import (
	"container/heap"
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"time"

	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	"github.com/arec1b0/traceblame/collector/internal/bench"
)

type loadFlags struct {
	params   bench.Params
	fixtures string
	seed     uint64
	runID    uint
	pod      uint
}

func (f *loadFlags) register(fs *flag.FlagSet) {
	fs.StringVar(&f.fixtures, "fixtures", "testdata/fixtures", "golden fixtures directory")
	fs.UintVar(&f.runID, "run-id", 0xbe57, "run id, encoded in every trace id")
	fs.UintVar(&f.pod, "pod", 0, "load generator index, encoded in every trace id")
	fs.IntVar(&f.params.SpansPerTrace, "spans", 4, "spans per trace (root + children)")
	fs.IntVar(&f.params.ErrorEvery, "error-every", 2, "every n-th trace carries an error span")
	fs.Uint64Var(&f.seed, "seed", 7, "generation seed")
}

func (f *loadFlags) generator() (*bench.Generator, error) {
	if f.runID > math.MaxUint32 || f.pod > math.MaxUint8 {
		return nil, fmt.Errorf("run-id %d or pod %d out of range", f.runID, f.pod)
	}
	f.params.Seed, f.params.RunID, f.params.Pod = f.seed, uint32(f.runID), uint8(f.pod)
	return bench.NewGenerator(f.params, f.fixtures)
}

// Summary is what a run prints last: one JSON line.
type Summary struct {
	RunID        uint32  `json:"run_id"`
	Pod          uint8   `json:"pod"`
	Tenant       string  `json:"tenant"`
	T0           int64   `json:"t0_ns"`
	Rate         float64 `json:"rate"`
	Traces       uint32  `json:"traces"`
	Spans        int     `json:"spans"`
	SentSpans    int     `json:"sent_spans"`
	UnsentSpans  int     `json:"unsent_spans"`
	ExportErrors int     `json:"export_errors"`
	FinishedNS   int64   `json:"finished_ns"`
	SpansPer     int     `json:"spans_per_trace"`
	ErrorEvery   int     `json:"error_every"`
	Seed         uint64  `json:"seed"`
}

type pending []bench.Batch

func (p pending) Len() int           { return len(p) }
func (p pending) Less(i, j int) bool { return p[i].SendAt < p[j].SendAt }
func (p pending) Swap(i, j int)      { p[i], p[j] = p[j], p[i] }
func (p *pending) Push(x any)        { *p = append(*p, x.(bench.Batch)) }
func (p *pending) Pop() any          { old := *p; b := old[len(old)-1]; *p = old[:len(old)-1]; return b }
func (p pending) nextAt() int64      { return p[0].SendAt }

func run(args []string, out io.Writer) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	var lf loadFlags
	lf.register(fs)
	endpoint := fs.String("endpoint", "otel-agent.observability.svc:4317", "agent OTLP/gRPC endpoint")
	user := fs.String("user", "", "basic auth user (the tenant)")
	password := fs.String("password", "", "basic auth password")
	rate := fs.Float64("rate", 100, "traces per second")
	duration := fs.Duration("duration", 6*time.Minute, "how long to start traces")
	giveUp := fs.Duration("give-up", 5*time.Minute, "retry a failed export this long after the load ends")
	if err := fs.Parse(args); err != nil {
		return err
	}
	g, err := lf.generator()
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient(*endpoint, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return err
	}
	defer func() { _ = conn.Close() }()
	client := ptraceotlp.NewGRPCClient(conn)
	auth := "Basic " + base64.StdEncoding.EncodeToString([]byte(*user+":"+*password))
	ctx := metadata.AppendToOutgoingContext(context.Background(), "authorization", auth)

	total := uint32(*rate * duration.Seconds())
	s := Summary{
		RunID: lf.params.RunID, Pod: lf.params.Pod, Tenant: *user, Rate: *rate, Traces: total,
		SpansPer: lf.params.SpansPerTrace, ErrorEvery: lf.params.ErrorEvery, Seed: lf.seed,
	}
	t0 := time.Now().UnixNano()
	s.T0 = t0
	interval := float64(time.Second) / *rate
	var queue pending
	next := uint32(0)
	deadline := t0 + duration.Nanoseconds() + giveUp.Nanoseconds()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for next < total || queue.Len() > 0 {
		now := (<-ticker.C).UnixNano()
		// A trace starts at its scheduled time, whatever the send time: timestamps stay
		// deterministic, so the analysis can place missing traces in time.
		for next < total && t0+int64(float64(next)*interval) <= now {
			for _, b := range g.Build(g.Trace(next), t0+int64(float64(next)*interval)) {
				s.Spans++
				heap.Push(&queue, b)
			}
			next++
		}
		batch := ptrace.NewTraces()
		for queue.Len() > 0 {
			if queue.nextAt() > now {
				break
			}
			b := heap.Pop(&queue).(bench.Batch)
			b.Traces.ResourceSpans().MoveAndAppendTo(batch.ResourceSpans())
		}
		if batch.SpanCount() == 0 {
			continue
		}
		n := batch.SpanCount()
		backoff := 200 * time.Millisecond
		for {
			_, err := client.Export(ctx, ptraceotlp.NewExportRequestFromTraces(batch))
			if err == nil {
				s.SentSpans += n
				break
			}
			s.ExportErrors++
			if time.Now().UnixNano() > deadline {
				s.UnsentSpans += n
				break
			}
			time.Sleep(backoff)
			backoff = min(2*backoff, 5*time.Second)
		}
	}
	s.FinishedNS = time.Now().UnixNano()
	return json.NewEncoder(out).Encode(s)
}

func probe(args []string, out io.Writer) error {
	fs := flag.NewFlagSet("probe", flag.ContinueOnError)
	addr := fs.String("addr", "", "host:port")
	timeout := fs.Duration("timeout", 5*time.Second, "connect timeout")
	if err := fs.Parse(args); err != nil {
		return err
	}
	conn, err := net.DialTimeout("tcp", *addr, *timeout)
	if err != nil {
		_, werr := fmt.Fprintf(out, "blocked %s: %v\n", *addr, err)
		return werr
	}
	_ = conn.Close()
	_, err = fmt.Fprintf(out, "connected %s\n", *addr)
	return err
}
