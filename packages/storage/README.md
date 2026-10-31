# traceblame-storage

Forward-only ClickHouse migrations and the storage contract ([ADR-0004](../../docs/adr/0004-clickhouse-schema-management.md)).

| Path | What it is |
|---|---|
| `src/traceblame_storage/migrations/clickhouse/` | `NNNN_<expand\|migrate\|contract>_<name>.sql`, applied in order |
| `src/traceblame_storage/contract/` | the statements code runs against the schema (N-1 test) |

```bash
uv run traceblame-storage check --base-ref origin/main     # phase rules, applied files unchanged
uv run traceblame-storage migrate --url http://user:pass@localhost:8123/traceblame
uv run traceblame-storage status --url http://user:pass@localhost:8123/traceblame
```

**Rules:**
- **Phases.** Expand creates and adds, migrate backfills, contract drops. A change is add,
  backfill, drop; never RENAME or MODIFY.
- **Idempotent.** Every statement carries `IF [NOT] EXISTS`, so a failed run is simply
  re-run.
- **Applied files never change.** `make contract` compares against `CONTRACT_BASE_REF`, and
  the runner refuses a changed applied file.

**The ingest path.** The collector's stock clickhouse exporter writes into
`otel_traces_ingest` (`ENGINE = Null`), and the materialized view `spans_from_otel` maps
each insert into `spans`.

**Tests:**
- `make test` runs the unit tests.
- `make integration` runs ClickHouse 26.8 LTS in a test container. It covers the
  migrations, the N-1 test, and the table-level no-content proof through the built
  collector.
