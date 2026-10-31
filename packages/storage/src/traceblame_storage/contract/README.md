# Storage contract statements

The reads and writes TraceBlame code relies on, one statement per file. The N-1 test runs
these statements from HEAD against the schema of the base revision, and the base revision's
statements against the HEAD schema (CLAUDE.md hard rule 4: code N works with schema N-1).
Add the statement here when a service starts to read or write a column.

- `spans_write.sql`: what the collector's clickhouse exporter v0.164.1 inserts (its column
  list, one normalized span)
- `*_read.sql` / `*_write.sql`: what the detectors, the attributor and the fault injector
  will read and write (design §1.1)

The trace-assembler's watermark scan (ADR-0003) is not here yet. It reads `trace_arrivals`,
which is new in this schema, so as a contract statement it would fail on schema N-1. It
lives in `tests/integration/trace_arrivals_scan.sql`, and the assembler (#35) moves it here
once the table is on main.

The fault injector's write with `injections.injection_id`, and the join of an injection to the
span it was applied at (`spans.attrs['aegis.fault.injection.id']`, #36), are not here either:
the column is new in this schema. They live in `tests/integration/injections_by_id_write.sql`
and `injections_by_id_read.sql`, and `faults/` (#14) moves them here once the column is on main.
