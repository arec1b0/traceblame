-- param eval_run_id = "faults-contract-1"
SELECT trace_id, eval_run_id, service, source, query_ref, stratum, golden_doc_ids, golden_status, planned_fault_kind, injection_id, seed
FROM eval_traces FINAL
WHERE eval_run_id = {eval_run_id:String}
ORDER BY trace_id
