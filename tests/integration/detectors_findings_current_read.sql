-- The current findings of a trace (#15): per (detector, detector_version), the rows of the run
-- that its newest detector_runs row names, by (revision, run_digest), and only if that run is
-- ok. A newer revision without a finding on a span hides the older revision's finding there; a
-- rerun with other context or another projection of the same revision selects only its own
-- rows; a failed run selects nothing (fail closed). The status filter is applied after FINAL
-- picked the newest marker. Embedded copy: traceblame_detectors/sql/ (the attributor, #17,
-- reads the same way).
-- param trace_id = "0af7651916cd43dd8448eb211c80319c"
SELECT toString(f.finding_id) AS finding_id,
    toString(f.span_id) AS span_id,
    f.detector AS detector,
    f.detector_version AS detector_version,
    toUnixTimestamp64Milli(f.revision) AS revision_ms,
    f.component AS component,
    f.severity AS severity,
    f.score AS score,
    f.details AS details
FROM findings AS f FINAL
INNER JOIN (
    SELECT detector, detector_version, revision, run_digest, status
    FROM detector_runs FINAL
    WHERE trace_id = {trace_id:FixedString(32)}
) AS r
    ON f.detector = r.detector
    AND f.detector_version = r.detector_version
    AND f.revision = r.revision
    AND f.run_digest = r.run_digest
WHERE f.trace_id = {trace_id:FixedString(32)}
    AND r.status = 'ok'
ORDER BY detector, detector_version, span_id
