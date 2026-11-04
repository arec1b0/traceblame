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

func isNoRows(err error) bool { return errors.Is(err, pgx.ErrNoRows) }
