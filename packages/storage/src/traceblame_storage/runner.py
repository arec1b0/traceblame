"""Apply forward-only migrations and record them in schema_migrations (ADR-0004)."""

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
            client.command(statement)
        client.insert("schema_migrations", [[m.version, m.name, m.phase, m.sha256]], _COLUMNS)
        done.append(m)
    return done
