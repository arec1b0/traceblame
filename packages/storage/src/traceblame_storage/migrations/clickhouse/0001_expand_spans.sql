-- Normalized spans (design §1.1), one row per span of profile aegis-telemetry/1.
-- Deviations from §1.1, recorded in ADR-0004:
--   span_kind also takes `embedding` (profile v1 has six kinds);
--   finish_reasons is the profile's string[] instead of one finish_reason;
--   decision_id, error_type, http_status_code, source_sdk, normalizer_version and
--   resource_attrs are added; a bloom filter index serves lookups by trace_id alone.
CREATE TABLE IF NOT EXISTS spans (
    trace_id FixedString(32),
    span_id FixedString(16),
    parent_span_id String,
    service LowCardinality(String),
    operation LowCardinality(String),
    span_kind LowCardinality(String),
    start_ts DateTime64(6),
    duration_ms Float64,
    status_code LowCardinality(String),
    provider LowCardinality(String),
    model LowCardinality(String),
    finish_reasons Array(LowCardinality(String)),
    input_tokens UInt32,
    output_tokens UInt32,
    tool_name LowCardinality(String),
    args_hash FixedString(16),
    retrieval_scores Array(Float32),
    retrieved_doc_ids Array(String),
    bundle_digest String,
    tenant_id LowCardinality(String),
    session_id String,
    content_ref String,
    attrs Map(LowCardinality(String), String),
    profile_version LowCardinality(String),
    decision_id String,
    error_type LowCardinality(String),
    http_status_code UInt16,
    source_sdk LowCardinality(String),
    normalizer_version LowCardinality(String),
    resource_attrs Map(LowCardinality(String), String),
    INDEX idx_trace_id trace_id TYPE bloom_filter(0.001) GRANULARITY 1
) ENGINE = MergeTree
PARTITION BY toDate(start_ts)
ORDER BY (service, trace_id, start_ts)
TTL toDate(start_ts) + INTERVAL 30 DAY;
