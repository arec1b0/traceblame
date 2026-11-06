-- The newest run of each detector version on a trace (#15): the stale-message check and the
-- reader's commit marker. Embedded copy: traceblame_detectors/sql/.
-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
SELECT detector,
    detector_version,
    toUnixTimestamp64Milli(revision) AS revision_ms,
    input_digest,
    context_digest,
    framework_version,
    status,
    error_kind,
    finding_count,
    findings_digest,
    run_digest
FROM detector_runs FINAL
WHERE trace_id = {trace_id:FixedString(32)}
