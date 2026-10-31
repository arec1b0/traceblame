-- Contract phase after #39: trace_arrivals (0009, 0010) replaced trace_roots as the
-- trace-assembler's watermark table, and nothing reads trace_roots. The view goes first, so
-- no insert lands in a table that is being dropped.
DROP VIEW IF EXISTS trace_roots_from_otel;
