-- param eval_run_id = "faults-contract-1"
-- param service = "agentic-rag"
-- param since = "2026-11-05 00:00:00"
-- Every trace of one app stored under an eval run's marker since the run began (#70): the
-- orphan sweep. A trace here that is neither in eval_traces nor in eval_exclusions fails the
-- run's check. Both apps of a run share its id, so the app's service narrows the read (and is
-- the first column of the sorting key).
SELECT trace_id, min(start_ts) AS first_start, arraySort(groupUniqArrayIf(attrs['aegis.fault.injection.id'], attrs['aegis.fault.injection.id'] != '')) AS markers, count() AS spans
FROM spans
WHERE service = {service:String} AND start_ts >= {since:DateTime64(6)} AND resource_attrs['aegis.eval.run.id'] = {eval_run_id:String}
GROUP BY trace_id
ORDER BY trace_id
