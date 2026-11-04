-- The trace-assembler's check of which traces of a batch already have a summary, and the
-- newest revision of each (any assembler version). trace_summary is new in this schema, so
-- this is not a contract statement yet; #15 moves it into contract/ with the first reader.
-- param trace_ids = ["0af7651916cd43dd8448eb211c80319c"]
SELECT toString(trace_id) AS tid,
    toUnixTimestamp64Milli(max(revision)) AS revision_ms
FROM trace_summary
WHERE trace_id IN {trace_ids:Array(String)}
GROUP BY trace_id
