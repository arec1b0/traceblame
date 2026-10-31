-- Root spans only, sorted by end time: the rows the trace-assembler's watermark query scans
-- ("root spans ended before now - grace", ADR-0003 draft). A table fed by a materialized
-- view rather than a projection on spans: a projection cannot filter rows, so it would copy
-- every span, while this keeps one row per trace, ordered by the watermark column.
-- ingested_at lets the assembler pick up roots that arrive after the watermark passed
-- their end_ts. ReplacingMergeTree absorbs a root span that is exported twice.
CREATE TABLE IF NOT EXISTS trace_roots (
    end_ts DateTime64(6),
    trace_id FixedString(32),
    root_span_id FixedString(16),
    service LowCardinality(String),
    tenant_id LowCardinality(String),
    start_ts DateTime64(6),
    status_code LowCardinality(String),
    ingested_at DateTime64(3) DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toDate(end_ts)
ORDER BY (end_ts, trace_id)
TTL toDate(end_ts) + INTERVAL 30 DAY;
