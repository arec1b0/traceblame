-- param trace_id = "1af7651916cd43dd8448eb211c80319c"
-- param eval_run_id = "faults-contract-1"
-- param service = "agentic-rag"
-- param entry_id = "agentic-rag:tool_failure:0123456789ab"
-- param attempt_id = "a001-20261117T100000Z"
-- param planned_fault_kind = "tool_failure"
-- param injection_id = "inj-contract-1"
-- param reason = "killed"
-- param source = "attempt_log"
INSERT INTO eval_exclusions (trace_id, eval_run_id, service, entry_id, attempt_id, planned_fault_kind, injection_id, reason, source, recorded_at)
SELECT {trace_id:FixedString(32)}, {eval_run_id:String}, {service:String}, {entry_id:String}, {attempt_id:String}, {planned_fault_kind:String}, {injection_id:String}, {reason:String}, {source:String}, now64(3)
