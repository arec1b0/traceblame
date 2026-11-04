-- Summary of an assembled trace (ADR-0003, #35): one row per (trace_id, assembler_version)
-- once merged. revision is the trace's last arrival (trace_arrivals.ingested_at): an
-- identical re-assembly writes the same row, one with newer spans supersedes it. Read it
-- with FINAL or argMax(..., revision). start_ts is the root's, so it is the same in every
-- revision and a row never changes partition (a ReplacingMergeTree dedups only inside one).
-- Counts, ids and timestamps only: no span content.
CREATE TABLE IF NOT EXISTS trace_summary (
    trace_id FixedString(32),
    assembler_version LowCardinality(String),
    revision DateTime64(3),
    assembled_at DateTime64(3) DEFAULT now64(3),
    service LowCardinality(String),
    tenant_id LowCardinality(String),
    root_span_id FixedString(16),
    root_status_code LowCardinality(String),
    start_ts DateTime64(6),
    duration_ms Float64,
    span_count UInt32,
    error_span_count UInt32,
    bundle_digest String,
    session_id String
) ENGINE = ReplacingMergeTree(revision)
PARTITION BY toDate(start_ts)
ORDER BY (trace_id, assembler_version)
TTL toDate(start_ts) + INTERVAL 30 DAY;
