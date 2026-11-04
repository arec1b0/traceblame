-- The trace-assembler's read of the spans of a batch of traces (ADR-0003 amendment). No
-- service filter: a trace's earlier half can have arrived in an earlier window. The bloom
-- filter on trace_id (ADR-0004 D7) and the start_ts bound, which prunes daily partitions,
-- keep it cheap. since is the look-back bound in epoch milliseconds.
-- param trace_ids = ["0af7651916cd43dd8448eb211c80319c"]
-- param since = 0
SELECT toString(trace_id) AS tid,
    toString(span_id) AS sid,
    parent_span_id,
    service,
    status_code,
    tenant_id,
    bundle_digest,
    session_id,
    toUnixTimestamp64Micro(start_ts) AS start_micros,
    duration_ms
FROM spans
WHERE trace_id IN {trace_ids:Array(String)}
    AND start_ts >= fromUnixTimestamp64Milli({since:Int64})
