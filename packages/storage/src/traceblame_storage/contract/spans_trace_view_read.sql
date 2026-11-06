-- The detectors' read of one trace's spans (#15). Only these columns reach a detector, and of
-- attrs only the keys in attr_keys: the union of the enabled detectors' declared reads, which
-- never holds an eval or fault marker, exception.* or content (traceblame_detectors.policy).
-- Not read at all: resource_attrs (holds aegis.eval.run.id), tenant_id, session_id,
-- decision_id, content_ref, normalizer_version. args_hash loses its NUL padding (a span without
-- one reads ''). since_ms is the look-back bound on start_ts in epoch milliseconds (prunes daily
-- partitions; the bloom filter on trace_id does the rest).
-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
-- param since_ms = 0
-- param attr_keys = ["aegis.output.parse_ok"]
SELECT toString(span_id) AS span_id,
    parent_span_id,
    service,
    operation,
    span_kind,
    toUnixTimestamp64Micro(start_ts) AS start_us,
    duration_ms,
    status_code,
    provider,
    model,
    finish_reasons,
    input_tokens,
    output_tokens,
    tool_name,
    replaceAll(toString(args_hash), '\0', '') AS args_hash,
    retrieval_scores,
    retrieved_doc_ids,
    bundle_digest,
    error_type,
    http_status_code,
    source_sdk,
    profile_version,
    mapFilter((k, v) -> has({attr_keys:Array(String)}, k), attrs) AS attrs
FROM spans
WHERE trace_id = {trace_id:FixedString(32)}
    AND start_ts >= fromUnixTimestamp64Milli({since_ms:Int64})
