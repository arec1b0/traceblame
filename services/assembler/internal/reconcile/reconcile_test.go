package reconcile

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"
)

const hourMs = int64(time.Hour / time.Millisecond)

type fakeChecker struct {
	now     int64
	missing uint64
	orphans uint64
	err     error
	h0, h1  int64
	since   int64
}

func (f *fakeChecker) Now(context.Context) (int64, error) { return f.now, f.err }
func (f *fakeChecker) Reconcile(_ context.Context, h0, h1, since int64) (uint64, uint64, error) {
	f.h0, f.h1, f.since = h0, h1, since
	return f.missing, f.orphans, f.err
}

type fakeReporter struct {
	got    []Result
	failed int
}

func (r *fakeReporter) Reconciled(x Result) { r.got = append(r.got, x) }
func (r *fakeReporter) ReconcileFailed()    { r.failed++ }

func TestTheWindowIsTheLatestHourThatClosedAtLeastTheMarginAgo(t *testing.T) {
	margin := 5 * time.Minute
	h0, h1 := Window(10*hourMs+4*60*1000, margin) // 10:04: hour 9..10 closed only 4 minutes ago
	if h0 != 8*hourMs || h1 != 9*hourMs {
		t.Fatalf("[%d, %d)", h0, h1)
	}
	h0, h1 = Window(10*hourMs+5*60*1000, margin) // 10:05: hour 9..10 is reconciled
	if h0 != 9*hourMs || h1 != 10*hourMs {
		t.Fatalf("[%d, %d)", h0, h1)
	}
}

func TestOnceReportsTheCountsOfTheClosedHour(t *testing.T) {
	c := &fakeChecker{now: 10*hourMs + 10*60*1000, missing: 2, orphans: 5}
	rep := &fakeReporter{}
	r, err := Once(context.Background(), c, rep, 5*time.Minute, 48*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if r.Missing != 2 || r.Orphans != 5 || len(rep.got) != 1 || rep.got[0] != r {
		t.Fatalf("%+v %+v", r, rep.got)
	}
	if c.h0 != 9*hourMs || c.h1 != 10*hourMs || c.since != 9*hourMs-48*hourMs {
		t.Fatalf("window [%d, %d) since %d", c.h0, c.h1, c.since)
	}
}

func TestAFailedPassIsCountedAndNeverFatal(t *testing.T) {
	c := &fakeChecker{err: errors.New("down")}
	rep := &fakeReporter{}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	Run(ctx, c, rep, 10*time.Millisecond, time.Minute, time.Hour, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	if rep.failed == 0 || len(rep.got) != 0 {
		t.Fatalf("failed=%d reported=%d", rep.failed, len(rep.got))
	}
}
