# Storage contract statements

The reads and writes TraceBlame code relies on, one statement per file. The N-1 test runs
these statements from HEAD against the schema of the base revision, and the base revision's
statements against the HEAD schema (CLAUDE.md hard rule 4: code N works with schema N-1).
Add the statement here when a service starts to read or write a column.

- `spans_write.sql`: what the collector's clickhouse exporter v0.164.1 inserts (its column
  list, one normalized span)
- `*_read.sql` / `*_write.sql`: what the detectors, the attributor and the fault injector
  will read and write (design §1.1)
- `trace_roots_read.sql`: the trace-assembler's watermark scan, root spans by end time
  (ADR-0003 draft, ADR-0004 section 4)
