"""Apply forward-only migrations and record them in schema_migrations (ADR-0004)."""

import re
from collections.abc import Sequence
from typing import Any, Protocol

from traceblame_storage.migrations import Migration, MigrationError

BOOKKEEPING = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version UInt32, name String, phase LowCardinality(String), sha256 FixedString(64), "
    "applied_at DateTime64(3) DEFAULT now64(3)"
    ") ENGINE = MergeTree ORDER BY version"
)
_COLUMNS = ["version", "name", "phase", "sha256"]


class Client(Protocol):
    def command(self, sql: str) -> object: ...

    def query_rows(self, sql: str) -> list[tuple[Any, ...]]: ...

    def insert(
        self, table: str, rows: Sequence[Sequence[Any]], column_names: Sequence[str]
    ) -> object: ...


class ChecksumMismatchError(MigrationError):
    pass


class DropGuardError(MigrationError):
    """ClickHouse refused a DROP above max_table_size_to_drop (error 359)."""


RUNBOOK = "docs/runbooks/clickhouse-drop-guard.md"
_GUARD_NAME = "TABLE_SIZE_EXCEEDS_MAX_DROP_SIZE_LIMIT"
_GUARD_CODE = re.compile(r"\bCode:\s*359\b")
_DROPPED_TABLE = re.compile(r"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\S+?)\s*;?\s*$", re.IGNORECASE)


def _is_drop_guard(error: Exception) -> bool:
    """Only recognises the error; nothing of the server's text is used afterwards."""
    text = str(error)
    return (
        getattr(error, "code", None) == 359 or _GUARD_NAME in text or bool(_GUARD_CODE.search(text))
    )


def _readable(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    value = float(n)
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        value /= 1000
        if value < 1000 or unit == "PB":
            return f"{value:.2f} {unit}"
    raise AssertionError("unreachable")


def _guard_facts(client: Client, table: str) -> tuple[str, str]:
    """(table label, "size S, limit L") from the server's tables, not from its error text."""
    name = table.rsplit(".", 1)[-1].strip('`"')
    # The name comes from the migration file's own DROP TABLE, not from the server.
    ((database, size),) = client.query_rows(
        "SELECT currentDatabase(), toUInt64(sum(bytes_on_disk)) FROM system.parts "  # noqa: S608
        f"WHERE active AND database = currentDatabase() AND table = '{name}'"
    )
    ((server,),) = client.query_rows(
        "SELECT value FROM system.server_settings WHERE name = 'max_table_size_to_drop'"
    )
    limit = int(server)
    ((session, changed),) = client.query_rows(
        "SELECT value, changed FROM system.settings WHERE name = 'max_table_size_to_drop'"
    )
    if int(changed):  # a query-level setting overrides the server's
        limit = int(session)
    limit_text = "unlimited" if limit == 0 else _readable(limit)
    return f"{database}.{name}", f"size {_readable(int(size))}, limit {limit_text}"


def _drop_guard(client: Client, migration: Migration, statement: str) -> DropGuardError:
    """The clear error for a refused drop; every fact comes from queries."""
    dropped = _DROPPED_TABLE.match(statement.strip())
    table = dropped.group(1) if dropped else None
    try:
        if table is None:
            raise ValueError("not a DROP TABLE statement")
        label, facts = _guard_facts(client, table)
    except Exception:
        label = table.rsplit(".", 1)[-1] if table else "?"
        facts = "size/limit unavailable"
    return DropGuardError(
        f"{migration.filename}: ClickHouse refused to drop table {label}: it is over"
        f" max_table_size_to_drop ({facts}). The migration is not recorded; fix the limit and"
        f" run migrate again (it applies only what is pending). See {RUNBOOK}."
    )


def _applied(client: Client) -> dict[int, str]:
    client.command(BOOKKEEPING)
    # toString: drivers return FixedString as bytes.
    rows = client.query_rows(
        "SELECT version, toString(sha256) FROM schema_migrations ORDER BY version"
    )
    return {int(v): str(s) for v, s in rows}


def status(client: Client, migrations: Sequence[Migration]) -> list[tuple[Migration, str]]:
    """Each migration with `applied`, `pending` or `changed`."""
    applied = _applied(client)
    out: list[tuple[Migration, str]] = []
    for m in migrations:
        sha = applied.get(m.version)
        out.append((m, "pending" if sha is None else "applied" if sha == m.sha256 else "changed"))
    return out


def migrate(client: Client, migrations: Sequence[Migration]) -> list[Migration]:
    """Apply every pending migration in order; refuse if an applied one changed."""
    applied = _applied(client)
    for m in migrations:
        sha = applied.get(m.version)
        if sha is not None and sha != m.sha256:
            raise ChecksumMismatchError(
                f"{m.filename} was applied with sha256 {sha} and now has {m.sha256}"
            )
    done: list[Migration] = []
    for m in sorted(migrations, key=lambda m: m.version):
        if m.version in applied:
            continue
        for statement in m.statements():
            try:
                client.command(statement)
            except Exception as error:
                if not _is_drop_guard(error):
                    raise
                raise _drop_guard(client, m, statement) from error
        client.insert("schema_migrations", [[m.version, m.name, m.phase, m.sha256]], _COLUMNS)
        done.append(m)
    return done
