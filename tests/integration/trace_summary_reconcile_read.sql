-- The trace-assembler's reconciliation of one closed window [h0, h1) (epoch ms) of
-- trace_arrivals against trace_summary (ADR-0003): missing = traces with a root arrival and no
-- summary; orphans = traces with arrivals, no root arrival and no summary. since bounds the
-- look-back on both tables. trace_summary is new in this schema, so this is not a contract
-- statement yet.
-- param h0 = 0
-- param h1 = 3600000
-- param since = 0
SELECT uniqExactIf(trace_id, is_root AND trace_id NOT IN (
        SELECT trace_id FROM trace_summary WHERE start_ts >= fromUnixTimestamp64Milli({since:Int64})
    )) AS missing,
    uniqExactIf(trace_id, trace_id NOT IN (
        SELECT trace_id FROM trace_arrivals
        WHERE is_root AND ingested_at >= fromUnixTimestamp64Milli({since:Int64})
    ) AND trace_id NOT IN (
        SELECT trace_id FROM trace_summary WHERE start_ts >= fromUnixTimestamp64Milli({since:Int64})
    )) AS orphans
FROM trace_arrivals
WHERE ingested_at >= fromUnixTimestamp64Milli({h0:Int64})
    AND ingested_at < fromUnixTimestamp64Milli({h1:Int64})
