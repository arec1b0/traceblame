-- Where the collector's stock clickhouse exporter writes (create_schema: false,
-- traces_table_name: otel_traces_ingest). The columns are exactly those of its INSERT
-- (exporter/clickhouseexporter v0.164.1, internal/sqltemplates/traces_insert.sql). The
-- Null engine stores nothing: spans_from_otel maps every insert into spans, and the status
-- message, events and links are never kept (ADR-0004).
CREATE TABLE IF NOT EXISTS otel_traces_ingest (
    Timestamp DateTime64(9),
    TraceId String,
    SpanId String,
    ParentSpanId String,
    TraceState String,
    SpanName LowCardinality(String),
    SpanKind LowCardinality(String),
    ServiceName LowCardinality(String),
    ResourceAttributes Map(LowCardinality(String), String),
    ScopeName String,
    ScopeVersion String,
    SpanAttributes Map(LowCardinality(String), String),
    Duration UInt64,
    StatusCode LowCardinality(String),
    StatusMessage String,
    Events Nested (
        Timestamp DateTime64(9),
        Name LowCardinality(String),
        Attributes Map(LowCardinality(String), String)
    ),
    Links Nested (
        TraceId String,
        SpanId String,
        TraceState String,
        Attributes Map(LowCardinality(String), String)
    )
) ENGINE = Null;
