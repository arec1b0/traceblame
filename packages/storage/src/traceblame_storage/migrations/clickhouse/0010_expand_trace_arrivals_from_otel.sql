-- Feeds trace_arrivals from every exporter insert: one row per trace, service and tenant
-- of the inserted block, is_root when the block holds the trace's root span. The tenant is a
-- resource attribute (the profile's scope; the normalizer drops it anywhere else).
CREATE MATERIALIZED VIEW IF NOT EXISTS trace_arrivals_from_otel TO trace_arrivals AS
SELECT
    toFixedString(substring(TraceId, 1, 32), 32) AS trace_id,
    now64(3) AS ingested_at,
    max(ParentSpanId = '') AS is_root,
    ServiceName AS service,
    ResourceAttributes['aegis.tenant.id'] AS tenant_id
FROM otel_traces_ingest
GROUP BY trace_id, service, tenant_id;
