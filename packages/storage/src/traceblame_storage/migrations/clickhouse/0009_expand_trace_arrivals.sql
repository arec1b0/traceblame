-- Span arrivals per trace: what the trace-assembler's watermark scan reads (ADR-0003, #39).
-- ingested_at is ClickHouse's clock when the exporter's insert lands. One scan, GROUP BY
-- trace_id over (watermark, now - grace]: a trace with a root arrival is assembled, an
-- assembled trace with new arrivals is re-assembled. Minimal columns: the trace, whether
-- the root arrived, the service (the leading key of spans) and the tenant; nothing else of
-- a span. The TTL covers only the assembly window (start value 2 days, ADR-0003), not the
-- 30 days of spans; by day, so a whole partition expires at once. Supersedes trace_roots
-- (0007), which the contract phase after #39 drops.
CREATE TABLE IF NOT EXISTS trace_arrivals (
    trace_id FixedString(32),
    ingested_at DateTime64(3) DEFAULT now64(3),
    is_root Bool,
    service LowCardinality(String),
    tenant_id LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toDate(ingested_at)
ORDER BY ingested_at
TTL toDate(ingested_at) + INTERVAL 2 DAY;
