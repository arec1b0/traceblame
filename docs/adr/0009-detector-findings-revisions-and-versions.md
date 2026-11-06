# ADR-0009: Detector findings: revisions, run markers and params-derived versions

- **Status:** Proposed
- **Date:** 2026-11-06
- **Issue:** #15 (design issue #13). The number is the next free one; it is assigned at merge.
- **Decider:** Daniil Krizhanovskyi

## Context

Design §1.1 keeps findings in one ClickHouse table, `ReplacingMergeTree(created_at)` ordered by
`(trace_id, detector, detector_version, span_id)`, idempotent by `(trace_id, detector,
detector_version)` (CLAUDE.md rule 3). Three facts do not fit that table alone:

- **Traces are re-assembled** (ADR-0003). A late span gives the trace a newer revision and a
  new `trace.completed`; detectors must treat it as replacing their earlier findings. With the
  design's key, a newer run that no longer finds anything on span A leaves the older finding on
  A in place, and an older revision redelivered after a newer one overwrites it (the version
  column is the write time).
- **Thresholds live in config** (rule 8) and #17 calibrates them. A threshold change under the
  same code version would reuse the same `detector_version` and hide the change.
- **Byte-identity and leakage need to be checkable.** Online and offline (#17 calibration on a
  frozen D1 snapshot) must give the same findings, and `findings.score` is Float32, so rows read
  back do not compare byte for byte.

## Decision

1. **Findings per revision.** `findings` gets `revision` (the `trace_summary` revision the
   detector ran on) and `input_digest` (sha256 of the detector's input). `revision` joins the
   sorting key: `(trace_id, detector, detector_version, span_id, revision)`. ClickHouse extends
   a sorting key, metadata only, with a column added in the same ALTER; the migration
   checker allows `MODIFY ORDER BY` in an expand ALTER only after an `ADD COLUMN` of the same
   statement. Rows of code N-1 read revision 0.
2. **A run marker.** `detector_runs`, `ReplacingMergeTree(revision)` ordered by `(trace_id,
   detector, detector_version)`, gets one row per detector run, written after the run's
   findings, also when it found nothing or failed: `input_digest`, `context_digest`,
   `findings_digest`, `framework_version`, `assembler_version`, `status`, `error_kind` (a fixed
   reason or an exception class name), `finding_count`. The current findings of a detector
   version are the rows whose `(revision, input_digest)` equal its newest run row. A crash
   between the two inserts leaves the previous revision current; the replay writes the same
   rows again.
3. **Params-derived versions.** `detector_version = <code semver>+p<first 8 hex of
   sha256(canonical behaviour params)>` (semver build metadata). Operational settings never
   enter it. `detector_params (detector, detector_version, code_version, framework_version,
   params)` resolves every version to its params; a writer refuses to start when a version is
   registered with other params.
4. **Deterministic ids and digests.** `finding_id = uuid5(trace_id/detector/detector_version/
   span_id/revision/input_digest)`; findings are canonical JSON (sorted keys, ASCII, no NaN).
   `FRAMEWORK_VERSION` covers the engine, the trace view, the canonical form, the policy and
   the context; version locks and golden vectors pin them and are append-only. Evidence
   strings are ids from the detector's own view, id-typed strings its own context returned
   (baseline bundle digests; golden doc ids for eval_only detectors in eval mode) or declared
   enum values; anything else fails the run.
5. **What a detector sees.** A trace view built from the contract reads of `trace_summary`
   and `spans`. The reads never select `resource_attrs`, tenant, session, decision or content
   references, and select from `attrs` only the keys enabled detectors declared, which can
   never be an eval or fault marker, `exception.*`, content or a correlation id. Eval-only
   context (golden doc ids) reaches only eval_only detectors in eval-mode offline runs, which
   write JSON lines only, never ClickHouse.

## Alternatives considered

- **Lightweight `DELETE` of older revisions.** Rejected: a mutation per re-assembled trace, and
  a crash between delete and insert loses findings.
- **One row per run with the findings as an array.** Rejected: it drops the design's per-span
  rows that the attributor and the API read.
- **`created_at` set to the revision.** Rejected: it abuses the write time, and a finding that
  disappears in a newer revision still stays.
- **A new findings table (`findings_v2`).** Rejected for now: a dual read and a contract phase
  for what one expand ALTER does.
- **Code semver only as the version.** Rejected: a forgotten bump after a threshold change is
  silent.

## Consequences

- **The old read changes meaning.** `contract/findings_read.sql` (code N-1) on the new schema
  returns one row per revision of a span: `revision` is in the sorting key, so `FINAL` no
  longer folds them, and the read does not select it
  (`tests/integration/test_detectors_live.py::test_the_old_findings_read_returns_one_row_per_revision`).
  On main its only caller is the storage contract harness (the statements and N-1 tests in
  `tests/integration/test_migrations_live.py`); no service reads findings yet. It is superseded
  by `detectors_findings_current_read.sql` and dropped from the contract when that read moves in.
- Older revisions' rows stay in `findings` (no TTL, like the design's table); readers filter
  through `detector_runs`. If they grow, a TTL or a contract-phase cleanup is a later change.
- The attributor (#17) and the API (#18) read findings through the run marker
  (`detectors_findings_current_read.sql`).
- The detectors' new statements live in `tests/integration/detectors_*.sql` with embedded
  copies until the schema is on main; then they move into the storage contract.
- PR-B (worker) adds `traceblame.finding.v1`, which references findings (ids, keys, scores) and
  carries the run's `input_digest`.
