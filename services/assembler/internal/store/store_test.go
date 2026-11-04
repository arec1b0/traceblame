package store

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
)

type fakeRow struct {
	epoch, wm int64
	err       error
}

func (r fakeRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	*dest[0].(*int64) = r.epoch
	*dest[1].(*int64) = r.wm
	return nil
}

type fakeDB struct {
	execSQL  string
	execArgs []any
	execN    int64
	execErr  error
	row      fakeRow
	rowSQL   string
	rowArgs  []any
}

func (d *fakeDB) Exec(_ context.Context, sql string, args ...any) (int64, error) {
	d.execSQL, d.execArgs = sql, args
	return d.execN, d.execErr
}

func (d *fakeDB) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	d.rowSQL, d.rowArgs = sql, args
	return d.row
}

func TestClaimReturnsTheLeaseAndPassesTheTTLInSeconds(t *testing.T) {
	db := &fakeDB{row: fakeRow{epoch: 7, wm: 1234}}
	l, ok, err := New(db, 30*time.Second).Claim(context.Background(), 4, 2, "r1")
	if err != nil || !ok || l != (poll.Lease{Partition: 2, Epoch: 7, WatermarkMs: 1234}) {
		t.Fatalf("%+v %v %v", l, ok, err)
	}
	if db.rowArgs[0] != 4 || db.rowArgs[1] != 2 || db.rowArgs[2] != "r1" || db.rowArgs[3] != 30.0 {
		t.Fatalf("args %v", db.rowArgs)
	}
}

func TestClaimOfAHeldPartitionIsNotAnError(t *testing.T) {
	db := &fakeDB{row: fakeRow{err: pgx.ErrNoRows}}
	_, ok, err := New(db, time.Second).Claim(context.Background(), 1, 0, "r1")
	if err != nil || ok {
		t.Fatalf("ok=%v err=%v", ok, err)
	}
}

func TestClaimWrapsADatabaseError(t *testing.T) {
	boom := errors.New("down")
	_, _, err := New(&fakeDB{row: fakeRow{err: boom}}, time.Second).Claim(context.Background(), 1, 0, "r1")
	if !errors.Is(err, boom) {
		t.Fatalf("%v", err)
	}
}

func TestAdvanceSucceedsOnlyWhenOneRowChanged(t *testing.T) {
	l := poll.Lease{Partition: 1, Epoch: 3, WatermarkMs: 100}
	db := &fakeDB{execN: 1}
	ok, err := New(db, 30*time.Second).Advance(context.Background(), 2, l, "r1", 200)
	if err != nil || !ok {
		t.Fatalf("ok=%v err=%v", ok, err)
	}
	want := []any{2, 1, "r1", int64(3), int64(200), 30.0, int64(100)}
	for i, w := range want {
		if db.execArgs[i] != w {
			t.Fatalf("arg %d = %v, want %v", i+1, db.execArgs[i], w)
		}
	}
	db.execN = 0
	if ok, _ := New(db, time.Second).Advance(context.Background(), 2, l, "r1", 200); ok {
		t.Fatal("no row changed: the lease was fenced")
	}
}

func TestInitPassesTheCountAndTheInitialWatermark(t *testing.T) {
	db := &fakeDB{}
	if err := New(db, time.Second).Init(context.Background(), 4, 55); err != nil {
		t.Fatal(err)
	}
	if db.execArgs[0] != 4 || db.execArgs[1] != int64(55) {
		t.Fatalf("args %v", db.execArgs)
	}
	db.execErr = errors.New("nope")
	if err := New(db, time.Second).Init(context.Background(), 4, 55); err == nil {
		t.Fatal("an error must surface")
	}
}

func TestTheStatementsFenceByEpochOwnerAndWatermark(t *testing.T) {
	for _, w := range []string{"AND owner = $3", "AND epoch = $4", "AND watermark_ms = $7"} {
		if !strings.Contains(advanceSQL, w) {
			t.Errorf("advance.sql lacks %q", w)
		}
	}
	if !strings.Contains(claimSQL, "lease_until <= now()") {
		t.Error("claim.sql must only take an expired or free lease")
	}
}
