-- One row per eval trace, clean or faulted (#14; ADR draft "Fault taxonomy, injection points
-- and eval ground truth"). Written by traceblame-faults record from the injector's ledger.
-- - query_ref: the dataset's own id (CUAD question id; runbook task '<task>@s<seed>'). #19
--   splits calibration/test by it (design §1.6).
-- - golden_doc_ids: chunk ids holding the answer; [] unless golden_status = 'mapped'
--   (mapped | unmapped | none). Read only by the eval-only feature golden_in_topk, never by
--   the production retrieval_miss path.
-- - planned_fault_kind '' = clean; injection_id '' = clean or not reached; otherwise it joins
--   injections and spans.attrs['aegis.fault.injection.id'].
-- ReplacingMergeTree: re-recording a ledger is idempotent; read with FINAL. No TTL, like
-- injections; spans expire after 30 days, so #19 exports a frozen D1 snapshot.
CREATE TABLE IF NOT EXISTS eval_traces (
    trace_id FixedString(32),
    eval_run_id String,
    service LowCardinality(String),
    source LowCardinality(String),
    query_ref String,
    stratum LowCardinality(String),
    golden_doc_ids Array(String),
    golden_status LowCardinality(String),
    planned_fault_kind LowCardinality(String),
    injection_id String,
    seed UInt32,
    recorded_at DateTime64(3)
) ENGINE = ReplacingMergeTree(recorded_at)
ORDER BY trace_id;
