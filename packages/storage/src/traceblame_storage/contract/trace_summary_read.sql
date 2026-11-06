-- The detectors' read of an assembled trace's summary (#15): the newest revision of the row the
-- event's assembler version wrote. Not read: tenant_id and session_id (detectors get no
-- correlation ids). revision_ms is the revision in epoch milliseconds, as in trace.completed.
-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
-- param assembler_version = "1.0.0"
SELECT toString(trace_id) AS trace_id,
    assembler_version,
    toUnixTimestamp64Milli(revision) AS revision_ms,
    service,
    toString(root_span_id) AS root_span_id,
    root_status_code,
    toUnixTimestamp64Micro(start_ts) AS start_us,
    duration_ms,
    span_count,
    error_span_count,
    bundle_digest
FROM trace_summary FINAL
WHERE trace_id = {trace_id:FixedString(32)}
    AND assembler_version = {assembler_version:String}
