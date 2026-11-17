-- Traces of an eval run that completed no plan entry (#70): the trace in flight when a run was
-- killed, a torn ledger line's trace, a trace an entry left behind without a record. Written by
-- traceblame-faults record from the run's attempt log (attempts.jsonl `abandoned` events);
-- such a trace is never written to eval_traces or injections, and nothing is deleted from
-- spans.
-- - Every reader of ground truth starts from eval_traces, so an abandoned trace is excluded by
--   construction; this table is the audit trail and what the checks compare against (no stored
--   trace of a run may be neither in eval_traces nor here).
-- - entry_id: the plan entry it was started for ('<app>:<kind|clean>:<sha256(query_ref)[:12]>');
--   the entry itself is completed by another trace. attempt_id: the attempt that ran it.
-- - injection_id: the entry's injection id ('' for a clean entry). The abandoned trace's spans
--   may carry it as aegis.fault.injection.id, next to the completed trace's: join ground truth
--   on eval_traces.trace_id, never on the injection id alone.
-- - reason: killed | failed | torn_tail | no_record. source: attempt_log | preload |
--   orphan_sweep (where the trace id came from).
-- ReplacingMergeTree: re-recording is idempotent; read with FINAL. No TTL, like eval_traces.
CREATE TABLE IF NOT EXISTS eval_exclusions (
    trace_id FixedString(32),
    eval_run_id String,
    service LowCardinality(String),
    entry_id String,
    attempt_id String,
    planned_fault_kind LowCardinality(String),
    injection_id String,
    reason LowCardinality(String),
    source LowCardinality(String),
    recorded_at DateTime64(3)
) ENGINE = ReplacingMergeTree(recorded_at)
ORDER BY (eval_run_id, trace_id);
