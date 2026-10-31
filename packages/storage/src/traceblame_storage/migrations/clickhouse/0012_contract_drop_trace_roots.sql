-- Contract phase after #39: drops trace_roots (0007), which nothing reads. ClickHouse refuses
-- to drop a MergeTree table over max_table_size_to_drop (default 50 GB, error 359). This
-- migration does not bypass the guard: check the size first and see
-- docs/runbooks/clickhouse-drop-guard.md. The runner reports a refused drop and records
-- nothing; run migrate again once the limit is dealt with.
DROP TABLE IF EXISTS trace_roots;
