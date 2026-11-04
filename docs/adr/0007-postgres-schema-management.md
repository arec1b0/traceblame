# ADR-0007: Postgres schema management: a second backend of the storage runner

- **Status:** Proposed
- **Date:** 2026-11-04
- **Issue:** #35 (the first Postgres table); ADR-0004 for the ClickHouse side
- **Decider:** Daniil Krizhanovskyi

## Context

Postgres holds control-plane state: the trace-assembler's watermark and partition leases
(ADR-0003, #35) and the replayer's `replay_jobs` state machine (design §1.1). The repo has no
Postgres migrations yet, and no tool for them.

Facts that shape the decision:
- **The rules are the same as for ClickHouse.** CLAUDE.md hard rule 4 is forward-only
  (expand → migrate → contract), with code N working on schema N−1, and it does not name a
  database.
- **The writers are in two languages.** The assembler is Go; the replayer and the API are
  Python. The schema needs one owner.
- **Postgres DDL is transactional**, unlike ClickHouse's. A migration can be applied or not
  applied as a unit.
- **Several replicas start at once** in a Deployment. ADR-0004 leaves concurrent ClickHouse
  runners unlocked and says to run one Job per release.

## Decision

Extend the runner in `packages/storage` (`traceblame-storage`) with a Postgres backend.

- **Files.** `migrations/postgres/NNNN_<expand|migrate|contract>_<name>.sql`, shipped as
  package data, versions contiguous, numbered independently of the ClickHouse ones.
- **Phase rules** (checked statically by `traceblame-storage check`, as for ClickHouse):
  - **expand:** `CREATE TABLE | INDEX IF NOT EXISTS` and `ALTER TABLE … ADD COLUMN IF NOT EXISTS`;
  - **migrate:** backfills, `INSERT … SELECT` and `UPDATE`;
  - **contract:** `DROP … IF EXISTS` only.

  `RENAME` and `ALTER COLUMN … TYPE` are never allowed: add, backfill, drop.
- **Applied files never change.** The runner records a sha256 per version in
  `schema_migrations` and refuses to run when an applied file changed; CI compares against
  `CONTRACT_BASE_REF`, as for ClickHouse.
- **One transaction per migration file.** The file and its `schema_migrations` row commit
  together, so a failed migration leaves nothing behind.
- **A run holds `pg_advisory_lock`** on a fixed key, so concurrent runners queue instead of
  racing.
- **Driver.** `psycopg` 3.
- **N−1.** The services' statements live in the storage contract, next to the ClickHouse ones,
  and the N−1 test runs them in both directions on the Postgres schema as it does for
  ClickHouse. A statement on a table that is new in the schema stays outside the contract until
  the table is on main (ADR-0004 section 2).
- **Callers.** Go and Python services only run statements; they never create tables. A
  release runs `traceblame-storage migrate` for both databases before the services roll.

## Alternatives considered

- **golang-migrate or goose.** Down migrations are their idiom, they have no phase rules, and
  they add a binary to pin, the objections of ADR-0004. They would also split the owners:
  Go migrations for the assembler, something else for the replayer.
- **Atlas.** Declarative diffs can emit destructive changes.
- **Each service migrates its own tables.** Two tools, two rule sets, and no single place to
  check the forward-only rule.

## Consequences

**Positive**
- One tool, one rule set and one `check` for both databases; the assembler needs no migration
  code.
- The advisory lock removes, for Postgres, the concurrent-runner gap that ADR-0004 accepts for
  ClickHouse.

**Negative and risks**
- The runner is ours to maintain, now for two backends.
- `pg_advisory_lock` is session-scoped: a pooler in transaction mode would break it, so the
  runner connects directly.
- The Python package carries a Postgres driver that only the runner and the Python services
  use.
