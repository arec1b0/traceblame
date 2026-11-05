-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
-- param eval_run_id = "faults-contract-1"
-- param service = "agentic-rag"
-- param source = "cuad"
-- param query_ref = "Contract A__Governing Law"
-- param stratum = "answerable"
-- param golden_doc_ids = ["cuad-0123456789ab-3"]
-- param golden_status = "mapped"
-- param planned_fault_kind = "context_truncation"
-- param injection_id = "inj-contract-1"
-- param seed = 7
INSERT INTO eval_traces (trace_id, eval_run_id, service, source, query_ref, stratum, golden_doc_ids, golden_status, planned_fault_kind, injection_id, seed, recorded_at)
SELECT {trace_id:FixedString(32)}, {eval_run_id:String}, {service:String}, {source:String}, {query_ref:String}, {stratum:String}, {golden_doc_ids:Array(String)}, {golden_status:String}, {planned_fault_kind:String}, {injection_id:String}, {seed:UInt32}, now64(3)
