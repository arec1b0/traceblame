-- Maps each exporter insert into spans. The exporter writes attribute values as strings:
-- numbers in decimal, arrays as JSON. Columns are read from the profile attributes; every
-- span attribute without a column stays in attrs, every resource attribute without one in
-- resource_attrs.
CREATE MATERIALIZED VIEW IF NOT EXISTS spans_from_otel TO spans AS
SELECT
    toFixedString(substring(TraceId, 1, 32), 32) AS trace_id,
    toFixedString(substring(SpanId, 1, 16), 16) AS span_id,
    ParentSpanId AS parent_span_id,
    ServiceName AS service,
    if(SpanAttributes['gen_ai.operation.name'] != '', SpanAttributes['gen_ai.operation.name'], SpanName) AS operation,
    SpanAttributes['aegis.span.kind'] AS span_kind,
    toDateTime64(Timestamp, 6) AS start_ts,
    Duration / 1e6 AS duration_ms,
    StatusCode AS status_code,
    SpanAttributes['gen_ai.provider.name'] AS provider,
    SpanAttributes['gen_ai.request.model'] AS model,
    JSONExtract(SpanAttributes['gen_ai.response.finish_reasons'], 'Array(String)') AS finish_reasons,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.input_tokens']) AS input_tokens,
    toUInt32OrZero(SpanAttributes['gen_ai.usage.output_tokens']) AS output_tokens,
    SpanAttributes['gen_ai.tool.name'] AS tool_name,
    toFixedString(substring(SpanAttributes['aegis.tool.args_hash'], 1, 16), 16) AS args_hash,
    JSONExtract(SpanAttributes['aegis.retrieval.scores'], 'Array(Float32)') AS retrieval_scores,
    JSONExtract(SpanAttributes['aegis.retrieval.doc_ids'], 'Array(String)') AS retrieved_doc_ids,
    SpanAttributes['aegis.bundle.digest'] AS bundle_digest,
    ResourceAttributes['aegis.tenant.id'] AS tenant_id,
    SpanAttributes['aegis.session.id'] AS session_id,
    SpanAttributes['aegis.content.ref'] AS content_ref,
    mapFilter((k, v) -> k NOT IN (
        'gen_ai.operation.name', 'aegis.span.kind', 'gen_ai.provider.name',
        'gen_ai.request.model', 'gen_ai.response.finish_reasons', 'gen_ai.usage.input_tokens',
        'gen_ai.usage.output_tokens', 'gen_ai.tool.name', 'aegis.tool.args_hash',
        'aegis.retrieval.scores', 'aegis.retrieval.doc_ids', 'aegis.bundle.digest',
        'aegis.session.id', 'aegis.content.ref', 'aegis.profile.version', 'aegis.decision.id',
        'error.type', 'http.response.status_code', 'aegis.source.sdk', 'aegis.normalizer.version'
    ), SpanAttributes) AS attrs,
    SpanAttributes['aegis.profile.version'] AS profile_version,
    SpanAttributes['aegis.decision.id'] AS decision_id,
    SpanAttributes['error.type'] AS error_type,
    toUInt16OrZero(SpanAttributes['http.response.status_code']) AS http_status_code,
    SpanAttributes['aegis.source.sdk'] AS source_sdk,
    SpanAttributes['aegis.normalizer.version'] AS normalizer_version,
    mapFilter((k, v) -> k NOT IN ('service.name', 'aegis.tenant.id'), ResourceAttributes) AS resource_attrs
FROM otel_traces_ingest;
