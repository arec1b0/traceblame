-- The current findings of a trace (#15): per (detector, detector_version), the rows of the
-- revision and input that its newest detector_runs row names. A newer revision without a
-- finding on a span hides the older revision's finding there. Embedded copy:
-- traceblame_detectors/sql/ (the attributor, #17, reads the same way).
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
    SELECT detector, detector_version, revision, input_digest
    FROM detector_runs FINAL
    WHERE trace_id = {trace_id:FixedString(32)}
) AS r
    ON f.detector = r.detector
    AND f.detector_version = r.detector_version
    AND f.revision = r.revision
    AND f.input_digest = r.input_digest
WHERE f.trace_id = {trace_id:FixedString(32)}
ORDER BY detector, detector_version, span_id
