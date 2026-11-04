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

type liveRow struct{ live int }

func (r liveRow) Scan(dest ...any) error {
	*dest[0].(*int) = r.live
	return nil
}

type fakeDB struct {
	execs       []string
	allExecArgs [][]any
	override    pgx.Row
	execSQL     string
	execArgs    []any
	execN       int64
	execErr     error
	row         fakeRow
	rowSQL      string
	rowArgs     []any
}

func (d *fakeDB) Exec(_ context.Context, sql string, args ...any) (int64, error) {
	d.execSQL, d.execArgs = sql, args
	d.execs = append(d.execs, sql)
	d.allExecArgs = append(d.allExecArgs, args)
	return d.execN, d.execErr
}

func (d *fakeDB) QueryRow(_ context.Context, sql string, args ...any) pgx.Row {
	d.rowSQL, d.rowArgs = sql, args
	if d.override != nil {
		return d.override
	}
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

func TestHeartbeatRegistersPrunesAndCountsTheLiveReplicas(t *testing.T) {
	db := &fakeDB{override: liveRow{live: 3}}
	live, err := New(db, 30*time.Second).Heartbeat(context.Background(), "r1")
	if err != nil || live != 3 {
		t.Fatalf("live=%d err=%v", live, err)
	}
	if len(db.execs) != 2 || db.execs[0] != heartbeatSQL || db.execs[1] != pruneReplicasSQL {
		t.Fatalf("statements %q", db.execs)
	}
	if db.rowSQL != liveReplicasSQL || db.rowArgs[0] != 30.0 {
		t.Fatalf("the live count uses the lease TTL: %q %v", db.rowSQL, db.rowArgs)
	}
	db.execErr = errors.New("down")
	if _, err := New(db, time.Second).Heartbeat(context.Background(), "r1"); err == nil {
		t.Fatal("a heartbeat error must surface")
	}
}

func TestLivenessIsDecidedByPostgresClockNotTheReplicas(t *testing.T) {
	// The statements stamp and compare with now() in SQL, and the replica sends only durations
	// and its name: no timestamp from its own clock reaches the database.
	for name, sql := range map[string]string{
		"heartbeat": heartbeatSQL, "live count": liveReplicasSQL, "prune": pruneReplicasSQL,
	} {
		if !strings.Contains(sql, "now()") {
			t.Errorf("%s does not use the database clock:\n%s", name, sql)
		}
	}
	if !strings.Contains(liveReplicasSQL, "heartbeat_at > now() - make_interval(") {
		t.Errorf("a replica is live when heartbeat_at > now() - ttl, decided in SQL:\n%s", liveReplicasSQL)
	}

	db := &fakeDB{override: liveRow{live: 1}}
	if _, err := New(db, 30*time.Second).Heartbeat(context.Background(), "r1"); err != nil {
		t.Fatal(err)
	}
	var all []any
	all = append(all, db.rowArgs...)
	for _, args := range db.allExecArgs {
		all = append(all, args...)
	}
	for _, a := range all {
		switch a.(type) {
		case string, float64, int, int64:
		default:
			t.Errorf("argument %v (%T) could carry the replica's clock", a, a)
		}
	}
}

func TestSilentReplicasArePrunedAfterTenLeaseTTLsInBoundedBatches(t *testing.T) {
	db := &fakeDB{override: liveRow{live: 1}}
	if _, err := New(db, 30*time.Second).Heartbeat(context.Background(), "r1"); err != nil {
		t.Fatal(err)
	}
	prune := db.allExecArgs[1]
	if prune[0] != 300.0 {
		t.Fatalf("prune age = %v s, want 10 x the 30 s lease TTL", prune[0])
	}
	if prune[1] != pruneBatch {
		t.Fatalf("prune batch = %v, want %d", prune[1], pruneBatch)
	}
	if !strings.Contains(pruneReplicasSQL, "LIMIT $2") {
		t.Errorf("the prune must be bounded:\n%s", pruneReplicasSQL)
	}
}

func TestReleaseIsFencedByTheOwnersEpoch(t *testing.T) {
	db := &fakeDB{}
	l := poll.Lease{Partition: 3, Epoch: 9, WatermarkMs: 100}
	if err := New(db, time.Second).Release(context.Background(), 4, l, "r1"); err != nil {
		t.Fatal(err)
	}
	want := []any{4, 3, "r1", int64(9)}
	for i, w := range want {
		if db.execArgs[i] != w {
			t.Fatalf("arg %d = %v, want %v", i+1, db.execArgs[i], w)
		}
	}
	for _, w := range []string{"AND owner = $3", "AND epoch = $4", "owner = NULL"} {
		if !strings.Contains(releaseSQL, w) {
			t.Errorf("release.sql lacks %q", w)
		}
	}
	if strings.Contains(releaseSQL, "watermark_ms") {
		t.Error("a release must keep the watermark")
	}
}
