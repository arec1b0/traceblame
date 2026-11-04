// Package config reads the trace-assembler's settings from the environment (CLAUDE.md rule 8:
// every threshold is configuration, none is hardcoded). A bad value fails at startup.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Version is the assembler's version, carried in every summary and event (rule 3). It is set at
// build time with -ldflags "-X .../internal/config.Version=...".
var Version = "1.0.0"

// PublishRetryBudget is how long a failed publish may be retried before the step gives up and
// the window is replayed. The duplicate window must outlast it.
const PublishRetryBudget = 30 * time.Second

// Config is the whole configuration.
type Config struct {
	// Thresholds (ADR-0003): starting hypotheses.
	PartitionCount   int
	Grace            time.Duration
	PollInterval     time.Duration
	MaxWindow        time.Duration
	BatchSize        int
	LeaseTTL         time.Duration
	Lookback         time.Duration // how far back a trace's spans are read
	InitialLookback  time.Duration // where a partition count with no earlier state starts
	ReconcileEvery   time.Duration
	ReconcileMargin  time.Duration // how long after an hour closes it is reconciled
	AssemblerVersion string

	// Streams.
	NATSURL         string
	NATSUser        string
	NATSPassword    string
	Stream          string
	Subject         string
	DuplicateWindow time.Duration
	StreamMaxAge    time.Duration
	StreamReplicas  int

	// Stores.
	ClickHouseAddr     string
	ClickHouseDatabase string
	ClickHouseUser     string
	ClickHousePassword string
	ClickHouseTLS      bool
	PostgresDSN        string

	// Process.
	Owner       string
	MetricsAddr string
}

// Load reads the environment through get (os.LookupEnv in main) and validates the result.
func Load(get func(string) (string, bool)) (Config, error) {
	r := &reader{get: get}
	host, _ := os.Hostname()
	c := Config{
		PartitionCount:   r.integer("ASSEMBLER_PARTITIONS", 2),
		Grace:            r.duration("ASSEMBLER_GRACE", 5*time.Second),
		PollInterval:     r.duration("ASSEMBLER_POLL_INTERVAL", 2*time.Second),
		MaxWindow:        r.duration("ASSEMBLER_MAX_WINDOW", 60*time.Second),
		BatchSize:        r.integer("ASSEMBLER_BATCH_SIZE", 500),
		LeaseTTL:         r.duration("ASSEMBLER_LEASE_TTL", 30*time.Second),
		Lookback:         r.duration("ASSEMBLER_LOOKBACK", 48*time.Hour),
		InitialLookback:  r.duration("ASSEMBLER_INITIAL_LOOKBACK", 48*time.Hour),
		ReconcileEvery:   r.duration("ASSEMBLER_RECONCILE_EVERY", 10*time.Minute),
		ReconcileMargin:  r.duration("ASSEMBLER_RECONCILE_MARGIN", 5*time.Minute),
		AssemblerVersion: r.str("ASSEMBLER_VERSION", Version),

		NATSURL:         r.str("NATS_URL", "nats://localhost:4222"),
		NATSUser:        r.str("NATS_USER", ""),
		NATSPassword:    r.str("NATS_PASSWORD", ""),
		Stream:          r.str("ASSEMBLER_STREAM", "TRACE_COMPLETED"),
		Subject:         r.str("ASSEMBLER_SUBJECT", "trace.completed"),
		DuplicateWindow: r.duration("ASSEMBLER_DUPLICATE_WINDOW", time.Hour),
		StreamMaxAge:    r.duration("ASSEMBLER_STREAM_MAX_AGE", 7*24*time.Hour),
		StreamReplicas:  r.integer("ASSEMBLER_STREAM_REPLICAS", 1),

		ClickHouseAddr:     r.str("CLICKHOUSE_ADDR", "localhost:9000"),
		ClickHouseDatabase: r.str("CLICKHOUSE_DATABASE", "default"),
		ClickHouseUser:     r.str("CLICKHOUSE_USER", "default"),
		ClickHousePassword: r.str("CLICKHOUSE_PASSWORD", ""),
		ClickHouseTLS:      r.boolean("CLICKHOUSE_TLS", false),
		PostgresDSN:        r.str("POSTGRES_DSN", ""),

		Owner:       r.str("ASSEMBLER_OWNER", host),
		MetricsAddr: r.str("ASSEMBLER_METRICS_ADDR", ":9464"),
	}
	if err := errors.Join(append(r.errs, c.Validate())...); err != nil {
		return Config{}, fmt.Errorf("config: %w", err)
	}
	return c, nil
}

// Validate checks the values against each other.
func (c Config) Validate() error {
	var errs []error
	check := func(ok bool, format string, args ...any) {
		if !ok {
			errs = append(errs, fmt.Errorf(format, args...))
		}
	}
	check(c.PartitionCount >= 1, "ASSEMBLER_PARTITIONS must be >= 1, got %d", c.PartitionCount)
	check(c.Grace > 0, "ASSEMBLER_GRACE must be > 0, got %s", c.Grace)
	check(c.PollInterval > 0, "ASSEMBLER_POLL_INTERVAL must be > 0, got %s", c.PollInterval)
	check(c.MaxWindow >= time.Second, "ASSEMBLER_MAX_WINDOW must be >= 1s, got %s", c.MaxWindow)
	check(c.BatchSize >= 1, "ASSEMBLER_BATCH_SIZE must be >= 1, got %d", c.BatchSize)
	check(c.Lookback >= c.MaxWindow, "ASSEMBLER_LOOKBACK (%s) must cover a window (%s)", c.Lookback, c.MaxWindow)
	check(c.InitialLookback > 0, "ASSEMBLER_INITIAL_LOOKBACK must be > 0, got %s", c.InitialLookback)
	check(c.LeaseTTL >= 3*c.PollInterval,
		"ASSEMBLER_LEASE_TTL (%s) must be at least 3 poll intervals (%s)", c.LeaseTTL, 3*c.PollInterval)
	// ADR-0003: a repeated publish must fall inside the duplicate window. The longest delay
	// between the first publish of a revision and its republish is the lease TTL (another owner
	// takes over), a poll interval and the publish retry budget.
	need := c.LeaseTTL + c.PollInterval + PublishRetryBudget
	check(c.DuplicateWindow >= need,
		"ASSEMBLER_DUPLICATE_WINDOW (%s) must be at least lease TTL + poll interval + publish retry budget (%s)",
		c.DuplicateWindow, need)
	check(c.StreamMaxAge >= c.DuplicateWindow,
		"ASSEMBLER_STREAM_MAX_AGE (%s) must not be shorter than the duplicate window (%s)", c.StreamMaxAge, c.DuplicateWindow)
	check(c.StreamReplicas >= 1, "ASSEMBLER_STREAM_REPLICAS must be >= 1, got %d", c.StreamReplicas)
	check(c.ReconcileEvery > 0, "ASSEMBLER_RECONCILE_EVERY must be > 0, got %s", c.ReconcileEvery)
	check(c.AssemblerVersion != "", "ASSEMBLER_VERSION must not be empty")
	check(c.Owner != "", "ASSEMBLER_OWNER must not be empty")
	check(c.Subject != "" && !strings.ContainsAny(c.Subject, " *>"), "ASSEMBLER_SUBJECT %q is not a plain subject", c.Subject)
	check(c.PostgresDSN != "", "POSTGRES_DSN is required")
	return errors.Join(errs...)
}

type reader struct {
	get  func(string) (string, bool)
	errs []error
}

func (r *reader) str(key, def string) string {
	if v, ok := r.get(key); ok {
		return v
	}
	return def
}

func (r *reader) integer(key string, def int) int {
	v, ok := r.get(key)
	if !ok {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		r.errs = append(r.errs, fmt.Errorf("%s: %q is not an integer", key, v))
		return def
	}
	return n
}

func (r *reader) duration(key string, def time.Duration) time.Duration {
	v, ok := r.get(key)
	if !ok {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		r.errs = append(r.errs, fmt.Errorf("%s: %q is not a duration", key, v))
		return def
	}
	return d
}

func (r *reader) boolean(key string, def bool) bool {
	v, ok := r.get(key)
	if !ok {
		return def
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		r.errs = append(r.errs, fmt.Errorf("%s: %q is not a boolean", key, v))
		return def
	}
	return b
}
