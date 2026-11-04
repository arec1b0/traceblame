-- The trace-assembler's watermark scan (ADR-0003): one GROUP BY trace_id over the window
-- (w0, w1] of arrival time, in epoch milliseconds on ClickHouse's clock, for one partition of
-- partition_count (a trace belongs to cityHash64(trace_id) % partition_count). A trace with a
-- root arrival is assembled; an assembled trace with new arrivals is re-assembled.
-- param w0 = 0
-- param w1 = 4102444800000
-- param partition_count = 1
-- param partition = 0
SELECT toString(trace_id) AS tid,
    max(is_root) AS has_root,
    toUnixTimestamp64Milli(max(ingested_at)) AS last_arrival_ms
FROM trace_arrivals
WHERE ingested_at > fromUnixTimestamp64Milli({w0:Int64})
    AND ingested_at <= fromUnixTimestamp64Milli({w1:Int64})
    AND cityHash64(trace_id) % {partition_count:UInt32} = {partition:UInt32}
GROUP BY trace_id
ORDER BY trace_id
