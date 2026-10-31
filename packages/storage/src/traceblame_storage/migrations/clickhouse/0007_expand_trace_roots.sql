-- Root spans in arrival order: the rows the trace-assembler's watermark query scans (ADR-0003).
-- The gateway's tail sampling releases a whole trace at its decision, so a root arrives with
-- its trace. ingested_at is ClickHouse's clock when the exporter's insert lands, the sort key
-- (ingested_at, trace_id) is the watermark's keyset, and the partition key prunes by day. A
-- table fed by a materialized view rather than a projection on spans: a projection cannot
-- filter rows, so it would copy every span, while this keeps one row per root span. Plain
-- MergeTree: a root exported twice is two rows, and the assembler's (re)assembly of a trace
-- is idempotent.
CREATE TABLE IF NOT EXISTS trace_roots (
    ingested_at DateTime64(3) DEFAULT now64(3),
    trace_id FixedString(32),
    root_span_id FixedString(16),
    service LowCardinality(String),
    tenant_id LowCardinality(String),
    start_ts DateTime64(6),
    end_ts DateTime64(6),
    status_code LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toDate(ingested_at)
ORDER BY (ingested_at, trace_id)
TTL toDate(ingested_at) + INTERVAL 30 DAY;
