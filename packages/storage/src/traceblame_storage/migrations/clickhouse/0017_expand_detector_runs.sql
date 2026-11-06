-- One row per detector run on a trace revision (#15), written after the run's findings: the
-- commit marker of the run, also when it found nothing or failed. ReplacingMergeTree on revision
-- keeps the newest revision of (trace_id, detector, detector_version) whatever the insert order.
-- - input_digest, context_digest: sha256 of the detector's input and of the read-only context
--   it was given; findings_digest: sha256 of the run's canonical findings (scores are Float32
--   in findings, so equality of runs is checked here, not on read-back rows).
-- - framework_version: the version of the engine, the trace view and the canonical form.
-- - error_kind: a fixed reason or an exception class name, never a message.
-- Ids, digests and counts only: no span content.
CREATE TABLE IF NOT EXISTS detector_runs (
    trace_id FixedString(32),
    detector LowCardinality(String),
    detector_version LowCardinality(String),
    revision DateTime64(3),
    input_digest String,
    context_digest String,
    framework_version LowCardinality(String),
    assembler_version LowCardinality(String),
    status Enum8('ok' = 1, 'error' = 2),
    error_kind LowCardinality(String),
    finding_count UInt16,
    findings_digest String,
    ran_at DateTime64(3)
) ENGINE = ReplacingMergeTree(revision)
ORDER BY (trace_id, detector, detector_version);
