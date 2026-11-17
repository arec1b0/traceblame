-- param eval_run_id = "faults-contract-1"
SELECT trace_id, eval_run_id, service, entry_id, attempt_id, planned_fault_kind, injection_id, reason, source
FROM eval_exclusions FINAL
WHERE eval_run_id = {eval_run_id:String}
ORDER BY trace_id
