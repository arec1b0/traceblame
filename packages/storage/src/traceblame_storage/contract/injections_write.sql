-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
-- param injection_id = "inj-contract-1"
-- param fault_kind = "context_truncation"
-- param target_component = "orchestration"
-- param params = "{}"
-- param seed = 7
-- param injected_at = "2026-11-05 12:00:00.000"
INSERT INTO injections (trace_id, injection_id, fault_kind, target_component, params, seed, injected_at)
SELECT {trace_id:FixedString(32)}, {injection_id:String}, {fault_kind:String}, {target_component:String}, {params:String}, {seed:UInt32}, {injected_at:DateTime64(3)}
WHERE NOT EXISTS (SELECT 1 FROM injections WHERE trace_id = {trace_id:FixedString(32)} AND injection_id = {injection_id:String})
