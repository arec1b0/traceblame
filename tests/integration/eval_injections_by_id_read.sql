-- param injection_ids = ["inj-contract-1"]
-- param trace_ids = ["0af7651916cd43dd8448eb211c80319c"]
-- Every injections row on one of these traces or with one of these injection ids (#70): the
-- check that an injection id has rows on its own trace only.
SELECT toString(trace_id) AS trace_id, injection_id, params
FROM injections
WHERE has({trace_ids:Array(String)}, toString(trace_id)) OR has({injection_ids:Array(String)}, injection_id)
ORDER BY trace_id, injection_id
