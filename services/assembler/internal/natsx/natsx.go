// Package natsx publishes trace.completed to NATS JetStream with publish-ack and
// Nats-Msg-Id, and creates the stream with the duplicate window ADR-0003 relies on.
package natsx

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
)

// StreamConfig is what the assembler needs of the stream.
type StreamConfig struct {
	Name            string
	Subject         string
	DuplicateWindow time.Duration
	MaxAge          time.Duration
	Replicas        int
}

// Connect opens the connection and the JetStream context.
func Connect(url, user, password string) (*nats.Conn, jetstream.JetStream, error) {
	opts := []nats.Option{nats.Name("traceblame-assembler"), nats.MaxReconnects(-1)}
	if user != "" {
		opts = append(opts, nats.UserInfo(user, password))
	}
	nc, err := nats.Connect(url, opts...)
	if err != nil {
		return nil, nil, fmt.Errorf("natsx: connect: %w", err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, nil, fmt.Errorf("natsx: jetstream: %w", err)
	}
	return nc, js, nil
}

// EnsureStream creates the stream or brings it to the configured duplicate window. The window
// is set explicitly: the server default (2 minutes) is shorter than a lease takeover.
func EnsureStream(ctx context.Context, js jetstream.JetStream, c StreamConfig) error {
	_, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:       c.Name,
		Subjects:   []string{c.Subject},
		Retention:  jetstream.LimitsPolicy,
		Storage:    jetstream.FileStorage,
		Duplicates: c.DuplicateWindow,
		MaxAge:     c.MaxAge,
		Replicas:   c.Replicas,
	})
	if err != nil {
		return fmt.Errorf("natsx: stream %s: %w", c.Name, err)
	}
	return nil
}

// jsPublisher is the part of jetstream.JetStream the publisher uses.
type jsPublisher interface {
	PublishMsg(ctx context.Context, msg *nats.Msg, opts ...jetstream.PublishOpt) (*jetstream.PubAck, error)
}

// Publisher implements poll.Publisher.
type Publisher struct {
	js      jsPublisher
	subject string
	budget  time.Duration
	sleep   func(context.Context, time.Duration) error
	now     func() time.Time
	// Duplicates counts acks the stream reported as duplicates (the dedup window at work).
	OnDuplicate func()
}

var _ poll.Publisher = (*Publisher)(nil)

// NewPublisher retries a failed publish for up to budget before giving up.
func NewPublisher(js jetstream.JetStream, subject string, budget time.Duration) *Publisher {
	return &Publisher{js: js, subject: subject, budget: budget, sleep: sleepCtx, now: time.Now}
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

// Publish sends the event and waits for the stream's ack. Nats-Msg-Id makes a repeat of the
// same revision a duplicate inside the stream's duplicate window.
func (p *Publisher) Publish(ctx context.Context, e poll.Event) error {
	data, err := json.Marshal(e)
	if err != nil {
		return err
	}
	deadline := p.now().Add(p.budget)
	wait := 100 * time.Millisecond
	for {
		msg := &nats.Msg{Subject: p.subject, Data: data, Header: nats.Header{}}
		msg.Header.Set(jetstream.MsgIDHeader, e.MsgID())
		ack, err := p.js.PublishMsg(ctx, msg)
		if err == nil {
			if ack.Duplicate && p.OnDuplicate != nil {
				p.OnDuplicate()
			}
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if !p.now().Add(wait).Before(deadline) {
			return fmt.Errorf("natsx: publish %s: %w", e.MsgID(), errors.Join(err, errBudget))
		}
		if serr := p.sleep(ctx, wait); serr != nil {
			return serr
		}
		wait = min(wait*2, 5*time.Second)
	}
}

var errBudget = errors.New("publish retry budget exhausted")
