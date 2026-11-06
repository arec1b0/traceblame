-- Findings per trace revision (#15; ADR draft "Detector findings: revisions, run markers and
-- params-derived versions"). A re-assembled trace (ADR-0003) is a new revision; its findings
-- are new rows next to the old revision's, so a newer run never merges with an older one.
-- - revision: the trace_summary revision the detector ran on. Rows of code N-1 get 0.
-- - input_digest: sha256 of the detector's input (its projection of the trace).
-- - run_digest: sha256 of the run's complete inputs (input and context digests, detector and
--   framework versions). It is in the sorting key, so the findings of another run of the same
--   revision (another projection, other context) never replace a committed run's rows.
-- The current findings of (trace_id, detector, detector_version) are the rows whose
-- (revision, run_digest) equal the detector_runs row of the same key, if that run is ok.
-- revision and run_digest are added without a default expression: ClickHouse extends a sorting
-- key only with columns added in the same ALTER and without a default (metadata-only change).
ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS revision DateTime64(3) AFTER detector_version,
    ADD COLUMN IF NOT EXISTS input_digest String AFTER revision,
    ADD COLUMN IF NOT EXISTS run_digest String AFTER input_digest,
    MODIFY ORDER BY (trace_id, detector, detector_version, span_id, revision, run_digest);
