-- Feeds trace_roots from every exporter insert, root spans only.
CREATE MATERIALIZED VIEW IF NOT EXISTS trace_roots_from_otel TO trace_roots AS
SELECT
    addMicroseconds(toDateTime64(Timestamp, 6), intDiv(Duration, 1000)) AS end_ts,
    toFixedString(substring(TraceId, 1, 32), 32) AS trace_id,
    toFixedString(substring(SpanId, 1, 16), 16) AS root_span_id,
    ServiceName AS service,
    ResourceAttributes['aegis.tenant.id'] AS tenant_id,
    toDateTime64(Timestamp, 6) AS start_ts,
    StatusCode AS status_code
FROM otel_traces_ingest
WHERE ParentSpanId = '';
