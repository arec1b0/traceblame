# Storage contract statements

The reads and writes TraceBlame code relies on, one statement per file. The N-1 test runs
these statements from HEAD against the schema of the base revision, and the base revision's
statements against the HEAD schema (CLAUDE.md hard rule 4: code N works with schema N-1).
Add the statement here when a service starts to read or write a column.

- `spans_write.sql`: what the collector's clickhouse exporter v0.164.1 inserts (its column
  list, one normalized span)
- `*_read.sql` / `*_write.sql`: what the detectors, the attributor and the fault injector
  will read and write (design §1.1)

- `trace_arrivals_scan.sql` and `spans_by_trace_read.sql`: the trace-assembler's watermark scan
  and its read of a batch of traces (ADR-0003, #35). They take ClickHouse query parameters
  (`{name:Type}`); the header lines `-- param <name> = <JSON value>` give the sample values
  that the N-1 run passes. The assembler embeds a copy of each (`services/assembler/internal/ch/sql/`,
  `scripts/sync-assembler-sql.sh`; a contract test fails when the copies differ).

- `trace_summary_read.sql` and `spans_trace_view_read.sql`: the detectors' read of an assembled
  trace (#15). The spans read selects of `attrs` only the keys in `attr_keys` and never
  `resource_attrs`, tenant, session, decision or content references
  (`traceblame_detectors.policy`).

The trace-assembler's other statements read or write `trace_summary` and `assembler_partitions`,
new in this schema, so they are not here yet (code N must run on schema N-1): the ClickHouse
ones live in `tests/integration/` and the Postgres ones in `services/assembler/internal/store/sql/`.
The first reader of `trace_summary` (#15) moves its statements here once the table is on main.

The fault injector (#14) runs two of these:
- `injections_write.sql` writes one injection with its `injection_id`, the value of
  `aegis.fault.injection.id` on the span it was applied at (#36). It inserts only when no row
  with the same `(trace_id, injection_id)` exists, so re-recording a ledger adds nothing.
- `injections_spans_read.sql` joins an injection to that span on
  `(trace_id, spans.attrs['aegis.fault.injection.id'] = injection_id)`.

Its statements on `eval_traces`, new in this schema, are not here yet: they live in
`tests/integration/eval_traces_write.sql` and `eval_traces_read.sql`. `traceblame-faults`
embeds a copy of each (`packages/faults/src/traceblame_faults/sql/`, checked by
`tests/contract/test_faults_sql_copies.py`). They move here once the table is on main.

A resumable fault run (#70) adds `eval_exclusions`, the traces of a run that completed no plan
entry (killed in flight, a torn ledger line, an entry that ended without a record). Its
statements and the eval reads are not here yet either: `tests/integration/eval_exclusions_write.sql`
and `eval_exclusions_read.sql`, `eval_dataset_read.sql` (the labelled traces of a run),
`eval_run_traces_read.sql` (every stored trace of one app under a run's marker: the orphan
sweep) and `eval_injections_by_id_read.sql`, embedded in `traceblame-faults` and checked by the
same contract test.
- **Join ground truth from `eval_traces.trace_id`, never on the injection id alone.** After a
  resume the spans of an abandoned trace carry the same `aegis.fault.injection.id` as the
  completed trace of the entry. `eval_dataset_read.sql` joins `injections` on
  `(trace_id, injection_id)` and is the only label read.
- An abandoned trace is never in `eval_traces` or `injections`: `record` writes one
  `eval_traces` row per ledger line, a ledger line exists only for a trace that has ended, and
  `record` refuses a ledger that names an abandoned trace.

The detectors (#15) write `findings` with the new `revision`, `input_digest` and `run_digest`
columns, and `detector_runs` and `detector_params`, all new in this schema: those statements
live in `tests/integration/detectors_*.sql`, embedded in `traceblame_detectors/sql/` (checked by
`tests/contract/test_detectors_contract.py`), until the schema is on main.

`findings_read.sql` predates revisions. On the new schema it returns one row per revision of a
span (`revision` is in the sorting key, so `FINAL` keeps each, and the read does not select it).
Its only caller is this contract's own harness; it is superseded by
`tests/integration/detectors_findings_current_read.sql` (ADR-0010 draft), which reads through
the `detector_runs` marker, and is replaced by it when that read moves here.
