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
