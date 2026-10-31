# ADR-0004: ClickHouse schema management and the span ingest path

- **Status:** Proposed
- **Date:** 2026-10-31
- **Issue:** #11 (design issue #9)
- **Decider:** Daniil Krizhanovskyi

## Context

Design §1.1 gives the DDL of `spans`, `findings`, `blame` and `injections`. CLAUDE.md hard
rule 4 makes migrations forward-only (expand → migrate → contract), with code N working on
schema N−1. Neither names a migration tool, or says how spans get from the collector into
`spans`.

Facts that shape the decision:
- **The exporter's shape is fixed.** The collector's stock `clickhouseexporter` (contrib
  v0.164.1, beta for traces) writes one fixed 22-column shape, `otel_traces`, including
  the status message, events and links. `create_schema: false` and `traces_table_name` let
  us own the table.
- **ClickHouse DDL is not transactional.** A half-applied migration has to be re-runnable.
- **§1.1 lags the profile.** It predates profile 1.1–1.3, and its `spans` table has no
  column for `aegis.decision.id` (a CLAUDE.md correlation id), `error.type`, the HTTP
  status (#29), `aegis.source.sdk` or `aegis.normalizer.version`.

## Decision

### 1. Ingest: stock exporter → Null table → materialized view

- **The ingest table.** The exporter writes into `otel_traces_ingest`, a table with exactly
  its INSERT columns and `ENGINE = Null`, so it stores nothing.
- **The view.** `spans_from_otel` (`TO spans`) maps every insert:
  - profile attributes into their columns;
  - every other span attribute into `attrs`;
  - every other resource attribute into `resource_attrs`.
- **No custom exporter.** The status message, events and links are never selected, so they
  never land on disk. That is one less place content can end up (ADR-0002).
- **Exporter upgrades.** An exporter release that changes its INSERT needs an expand
  migration of `otel_traces_ingest` first. The N−1 test and the integration test with the
  real exporter catch a mismatch.

### 2. Migrations: an in-house, forward-only runner

The runner lives in `packages/storage` (`traceblame-storage`, clickhouse-connect over HTTP).

- **Files.** One file per migration, `NNNN_<expand|migrate|contract>_<name>.sql`, shipped as
  package data. Versions are contiguous.
- **What each phase may contain** (checked statically by `traceblame-storage check` and the
  contract tests):
  - **expand:** `CREATE … IF NOT EXISTS` and `ALTER TABLE … ADD … IF NOT EXISTS`;
  - **migrate:** backfills: `INSERT … SELECT`, `ALTER TABLE … UPDATE | MATERIALIZE …`;
  - **contract:** `DROP … IF EXISTS` only.

  RENAME and MODIFY are never allowed. A change is expressed as add, then backfill, then drop.
- **Applied files never change.** Every migration on the base revision stays, byte for byte;
  CI compares against `CONTRACT_BASE_REF`. The runner records a sha256 per version in
  `schema_migrations` and refuses to run when an applied file changed.
- **N−1.** Two runs, both against the storage contract (`contract/*.sql`: the exporter's
  INSERT and the reads and writes of the services):
  - HEAD's contract statements on the base revision's schema (code N on schema N−1);
  - the base revision's statements on HEAD's schema.

  It applies from the first schema change after this one.

Alternatives considered:
- **golang-migrate or goose.** Down migrations are their idiom; they have no phase rules;
  and they add a binary to pin when the readers (detectors, attributor, API) are Python.
- **Atlas.** Declarative diffs can emit destructive changes, and its ClickHouse support
  is commercial-tier.

### 3. Deviations from the §1.1 DDL

| Tag | Change | Why |
|---|---|---|
| D1 | `span_kind` also takes `embedding` | profile v1 has six span kinds |
| D2 | `finish_reasons Array(LowCardinality(String))` instead of `finish_reason` | the profile has an array; one column would lose choices (`has(finish_reasons,'length')`) |
| D3 | `decision_id String` | CLAUDE.md correlation id missing from §1.1 |
| D4 | `error_type`, `http_status_code UInt16` (0 = none) | what `tool_error_propagation` and `provider_degradation` key on (#29) |
| D5 | `source_sdk`, `normalizer_version` | reproducible blame (profile 1.1.0) |
| D6 | `resource_attrs Map` | resource attributes without a column (e.g. `service.version`) are kept |
| D7 | bloom filter index on `trace_id` | lookups by trace id without `service` |
| D8 | `otel_traces_ingest` (Null) and `spans_from_otel` | section 1 |
| D9 | `schema_migrations` | section 2 |
| D10 | `operation` = `gen_ai.operation.name`, else the span name | §1.1 does not say |
| D11 | `model` = `gen_ai.request.model`; the response model stays in `attrs` | required by the profile |
| D12 | `trace_roots` (`MergeTree`, `ORDER BY (ingested_at, trace_id)`) and `trace_roots_from_otel` | the first watermark table (#11); superseded by D13; dropped by 0011 and 0012 (#41) |
| D13 | `trace_arrivals` (`MergeTree`, `ORDER BY ingested_at`, TTL 2 d) and `trace_arrivals_from_otel` (#39) | the trace-assembler's watermark scan; section 4 |

`findings`, `blame` and `injections` are as in §1.1, with `IF NOT EXISTS`.

### 4. Arrivals for the trace-assembler's watermark

ADR-0003 (Proposed) has the trace-assembler poll ClickHouse with a persisted watermark
instead of consuming `spans.sampled`. #11 shipped `trace_roots` (root spans only, D12). #39
replaces it with `trace_arrivals` (D13) as a forward-only expand step (0009, 0010). The
contract phase (#41: 0011 drops the view, 0012 the table) removed `trace_roots`. Each poll is one scan on arrival time:
`GROUP BY trace_id` over `(watermark, now − grace]`. A trace with a root arrival is
assembled; an assembled trace with new arrivals is re-assembled. `spans` is sorted by
`(service, trace_id, start_ts)`, so that scan would read every recent span. Two options:

- **A projection on `spans`.** Rejected: a projection cannot filter or aggregate columns
  away, so it would store a second copy of every span just to sort them by arrival.
- **`trace_arrivals`, fed by a materialized view.** Chosen.
  - **Rows.** One per trace, service and tenant of each exporter insert.
  - **Columns.** Only `trace_id`, `ingested_at` (`now64(3)` at insert), `is_root`, `service`
    and `tenant_id`: nothing else of a span, so no content.
  - **Order.** `ORDER BY ingested_at`, `PARTITION BY toDate(ingested_at)`. The integration
    test checks `EXPLAIN indexes = 1` with a root arrival and a late non-root arrival: the
    min-max index prunes the parts before the watermark, and the primary key is searched
    binary on `ingested_at`.
  - **Engine.** Plain `MergeTree`. Several rows per trace are the point: the scan aggregates
    them.
  - **TTL.** 2 days, by arrival day: the assembly window only, not the 30 days of `spans`
    (ADR-0003).

## Consequences

**Positive**
- The collector stays stock, and the schema is versioned, idempotent and checked in CI.
- The table-level no-content proof runs on the real exporter:
  - every fixture, content-on ones included, through the built collector into ClickHouse;
  - no row of any table holds the capture's text;
  - it fails as soon as the normalizer is bypassed.

**Negative and risks**
- Contract-phase drops meet ClickHouse's `max_table_size_to_drop` (default 50 GB, error 359).
  Migrations do not bypass it: the runner reports a refused drop with the migration, table,
  size and limit, records nothing, and points to `docs/runbooks/clickhouse-drop-guard.md`.
- The runner is ours to maintain.
- Concurrent runners against one database are not locked against each other: run one Job
  per release.
- Values in `attrs` and `resource_attrs` are the exporter's strings (numbers in decimal,
  arrays as JSON).
- The exporter's column set couples migrations to the exporter version.
