-- param eval_run_id = "faults-contract-1"
-- The labelled traces of an eval run (#70): one row per eval_traces row, with its injection
-- joined on (trace_id, injection_id). It starts from eval_traces.trace_id and never joins on
-- the injection id alone: the spans of an abandoned trace carry the same injection id, and an
-- injections row for another trace must not label this one. injection_rows = 0 for a clean or
-- not-reached trace.
SELECT e.trace_id, e.service, e.source, e.query_ref, e.stratum, e.planned_fault_kind, e.injection_id, i.applied_kind, i.applied_component, i.injection_rows, i.variants
FROM (
    SELECT trace_id, service, source, query_ref, stratum, planned_fault_kind, injection_id
    FROM eval_traces FINAL
    WHERE eval_run_id = {eval_run_id:String}
) AS e
LEFT JOIN (
    SELECT trace_id, injection_id, any(fault_kind) AS applied_kind, any(target_component) AS applied_component, count() AS injection_rows, uniqExact(fault_kind, target_component) AS variants
    FROM injections
    WHERE injection_id != '' AND trace_id IN (SELECT trace_id FROM eval_traces FINAL WHERE eval_run_id = {eval_run_id:String})
    GROUP BY trace_id, injection_id
) AS i ON i.trace_id = e.trace_id AND i.injection_id = e.injection_id
ORDER BY e.trace_id
