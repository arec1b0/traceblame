// Package store is the assembler's Postgres side: the per-partition watermark and lease
// (assembler_partitions). The statements are the files in sql/, tested against a real Postgres.
package store

import (
	"context"
	_ "embed"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/arec1b0/traceblame/services/assembler/internal/poll"
)

//go:embed sql/init.sql
var initSQL string

//go:embed sql/claim.sql
var claimSQL string

//go:embed sql/advance.sql
var advanceSQL string

//go:embed sql/heartbeat.sql
var heartbeatSQL string

//go:embed sql/prune_replicas.sql
var pruneReplicasSQL string

//go:embed sql/live_replicas.sql
var liveReplicasSQL string

//go:embed sql/release.sql
var releaseSQL string

const (
	// pruneTTLs: a silent replica's row is kept for this many lease TTLs before it is deleted.
	pruneTTLs = 10
	// pruneBatch bounds one prune; the next heartbeat takes the rest.
	pruneBatch = 100
)

// DB is the part of a pgx pool the store uses.
type DB interface {
	Exec(ctx context.Context, sql string, args ...any) (rowsAffected int64, err error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Store implements poll.Watermarks.
type Store struct {
	db  DB
	ttl time.Duration
}

var _ poll.Watermarks = (*Store)(nil)

// New wraps a database handle; ttl is the lease length every claim and advance renews to.
func New(db DB, leaseTTL time.Duration) *Store { return &Store{db: db, ttl: leaseTTL} }

// Open connects a pool.
func Open(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("store: connect: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("store: ping: %w", err)
	}
	return pool, nil
}

// PoolDB adapts a pool to DB.
type PoolDB struct{ Pool *pgxpool.Pool }

// Exec runs a statement and reports the rows it changed.
func (p PoolDB) Exec(ctx context.Context, sql string, args ...any) (int64, error) {
	tag, err := p.Pool.Exec(ctx, sql, args...)
	return tag.RowsAffected(), err
}

// QueryRow runs a query that returns one row.
func (p PoolDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	return p.Pool.QueryRow(ctx, sql, args...)
}

// Init creates the missing partitions of partitionCount.
func (s *Store) Init(ctx context.Context, partitionCount int, initialMs int64) error {
	if _, err := s.db.Exec(ctx, initSQL, partitionCount, initialMs); err != nil {
		return fmt.Errorf("store: init partitions: %w", err)
	}
	return nil
}

// Claim takes or renews a partition's lease; ok is false while another owner's lease is live.
func (s *Store) Claim(ctx context.Context, partitionCount, partition int, owner string) (poll.Lease, bool, error) {
	l := poll.Lease{Partition: partition}
	err := s.db.QueryRow(ctx, claimSQL, partitionCount, partition, owner, s.ttl.Seconds()).
		Scan(&l.Epoch, &l.WatermarkMs)
	switch {
	case err == nil:
		return l, true, nil
	case isNoRows(err):
		return poll.Lease{}, false, nil
	default:
		return poll.Lease{}, false, fmt.Errorf("store: claim: %w", err)
	}
}

// Advance moves the watermark if the lease's epoch is still the owner's.
func (s *Store) Advance(ctx context.Context, partitionCount int, l poll.Lease, owner string, newMs int64) (bool, error) {
	n, err := s.db.Exec(ctx, advanceSQL, partitionCount, l.Partition, owner, l.Epoch, newMs, s.ttl.Seconds(), l.WatermarkMs)
	if err != nil {
		return false, fmt.Errorf("store: advance: %w", err)
	}
	return n == 1, nil
}

// Heartbeat registers the replica and returns how many replicas heartbeated within the lease TTL.
func (s *Store) Heartbeat(ctx context.Context, owner string) (int, error) {
	if _, err := s.db.Exec(ctx, heartbeatSQL, owner); err != nil {
		return 0, fmt.Errorf("store: heartbeat: %w", err)
	}
	if _, err := s.db.Exec(ctx, pruneReplicasSQL, pruneTTLs*s.ttl.Seconds(), pruneBatch); err != nil {
		return 0, fmt.Errorf("store: prune replicas: %w", err)
	}
	var live int
	if err := s.db.QueryRow(ctx, liveReplicasSQL, s.ttl.Seconds()).Scan(&live); err != nil {
		return 0, fmt.Errorf("store: live replicas: %w", err)
	}
	return live, nil
}

// Release gives a partition up if the lease is still the owner's.
func (s *Store) Release(ctx context.Context, partitionCount int, l poll.Lease, owner string) error {
	if _, err := s.db.Exec(ctx, releaseSQL, partitionCount, l.Partition, owner, l.Epoch); err != nil {
		return fmt.Errorf("store: release: %w", err)
	}
	return nil
}

func isNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }
