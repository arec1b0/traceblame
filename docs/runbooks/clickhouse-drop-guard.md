# ClickHouse refuses a DROP: max_table_size_to_drop

**Symptom.** `traceblame-storage migrate` stops with
`<migration>: ClickHouse refused to drop table <db>.<table> (size <size>, limit <limit>)`.
This is ClickHouse error 359, `TABLE_SIZE_EXCEEDS_MAX_DROP_SIZE_LIMIT`.

The size and the limit in the message are queried by the runner (`system.parts`,
`system.server_settings`, and the session value of `system.settings` when one is set), not taken
from the server's error text. If the queries fail, the message says `size/limit unavailable`:
run step 1 yourself.

**Why.** ClickHouse refuses `DROP TABLE` and `TRUNCATE` of a MergeTree table larger than
`max_table_size_to_drop` (default 50 GB; `0` means no limit). Contract-phase migrations drop
tables, and ADR-0004 keeps them from bypassing the guard: an oversized drop is a decision for
whoever runs it. The first such migration is 0012 (`trace_roots`, issue #41).

**State after the failure.** Nothing of the refused migration is recorded in
`schema_migrations`; earlier migrations stay applied. `migrate` is safe to run again and
applies only what is pending.

## Steps

1. **Check the size first** and compare it with the limit:
   ```sql
   SELECT formatReadableSize(sum(bytes_on_disk)) AS size, sum(bytes_on_disk) AS bytes
   FROM system.parts
   WHERE database = currentDatabase() AND table = '<table>' AND active;

   SELECT value FROM system.server_settings WHERE name = 'max_table_size_to_drop';
   ```
2. **Is the drop still wanted?** The migration's header says what reads the table. If something
   still reads it, stop and raise it; do not lift the guard.
3. **Pick the narrowest way to proceed:**
   1. *Let the TTL shrink the table*, if it is close to the limit (`trace_roots` expires after
      30 days). Run `migrate` later.
   2. *Raise the limit for one session*, which needs no restart. Run the drop with
      `SETTINGS max_table_size_to_drop = 0` (or a value above the size) in a session of its
      own, or `SET max_table_size_to_drop = 0` and then `traceblame-storage migrate` from that
      same session.
   3. *The flag file*: create `<clickhouse-path>/flags/force_drop_table` on the server. ClickHouse
      removes it when the drop uses it (it needs write permission on the file), so it lifts the
      guard for one drop; remove it yourself if it was not consumed.
4. Run `uv run traceblame-storage migrate` again, then `status` to see every version applied.

Do not edit the migration to add the setting: applied migrations are immutable (their sha256 is
checked), and the guard exists to stop exactly this drop from happening unreviewed.
