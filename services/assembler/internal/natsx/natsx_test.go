package natsx

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
)

type fakeJS struct {
	msgs     []*nats.Msg
	failures int
	dup      bool
}

func (f *fakeJS) PublishMsg(_ context.Context, m *nats.Msg, _ ...jetstream.PublishOpt) (*jetstream.PubAck, error) {
	f.msgs = append(f.msgs, m)
	if f.failures > 0 {
		f.failures--
		return nil, errors.New("no responders")
	}
	return &jetstream.PubAck{Duplicate: f.dup}, nil
}

func newTestPublisher(js *fakeJS, budget time.Duration) (*Publisher, *time.Duration) {
	var slept time.Duration
	clock := time.Unix(0, 0)
	p := &Publisher{js: js, subject: "trace.completed", budget: budget}
	p.now = func() time.Time { return clock }
	p.sleep = func(_ context.Context, d time.Duration) error { slept += d; clock = clock.Add(d); return nil }
	return p, &slept
}

var event = poll.Event{TraceID: "4bf92f3577b34da6a3ce929d0e0e4736", Revision: 1793700000123, SpanCount: 7, AssemblerVersion: "1.0.0"}

func TestThePayloadIsTheContractsGoldenAndTheMsgIDHeaderIsSet(t *testing.T) {
	js := &fakeJS{}
	p, _ := newTestPublisher(js, time.Minute)
	if err := p.Publish(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	golden, err := os.ReadFile("../../testdata/trace_completed.golden.json")
	if err != nil {
		t.Fatal(err)
	}
	if got, want := string(js.msgs[0].Data), strings.TrimSpace(string(golden)); got != want {
		t.Fatalf("payload\n got %s\nwant %s", got, want)
	}
	if got := js.msgs[0].Header.Get(jetstream.MsgIDHeader); got != event.MsgID() {
		t.Fatalf("Nats-Msg-Id = %q, want %q", got, event.MsgID())
	}
	if js.msgs[0].Subject != "trace.completed" {
		t.Fatalf("subject %q", js.msgs[0].Subject)
	}
}

func TestAFailedPublishIsRetriedWithTheSameMsgIDUntilItIsAcked(t *testing.T) {
	js := &fakeJS{failures: 3}
	p, slept := newTestPublisher(js, time.Minute)
	if err := p.Publish(context.Background(), event); err != nil {
		t.Fatal(err)
	}
	if len(js.msgs) != 4 {
		t.Fatalf("attempts = %d", len(js.msgs))
	}
	for _, m := range js.msgs {
		if m.Header.Get(jetstream.MsgIDHeader) != event.MsgID() {
			t.Fatal("every attempt carries the same Nats-Msg-Id")
		}
	}
	if *slept != 100*time.Millisecond+200*time.Millisecond+400*time.Millisecond {
		t.Fatalf("backoff %s", *slept)
	}
}

func TestThePublishGivesUpWhenTheRetryBudgetIsSpent(t *testing.T) {
	js := &fakeJS{failures: 1000}
	p, slept := newTestPublisher(js, 3*time.Second)
	err := p.Publish(context.Background(), event)
	if err == nil || !errors.Is(err, errBudget) {
		t.Fatalf("want the budget error, got %v", err)
	}
	if *slept > 3*time.Second {
		t.Fatalf("slept %s past the budget", *slept)
	}
}

func TestADuplicateAckIsSuccessAndIsCounted(t *testing.T) {
	js := &fakeJS{dup: true}
	p, _ := newTestPublisher(js, time.Minute)
	n := 0
	p.OnDuplicate = func() { n++ }
	if err := p.Publish(context.Background(), event); err != nil || n != 1 {
		t.Fatalf("err=%v duplicates=%d", err, n)
	}
}

func TestACancelledContextStopsTheRetries(t *testing.T) {
	js := &fakeJS{failures: 1000}
	p, _ := newTestPublisher(js, time.Hour)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := p.Publish(ctx, event); !errors.Is(err, context.Canceled) {
		t.Fatalf("%v", err)
	}
}
