"""Apply forward-only Postgres migrations and record them in schema_migrations (ADR-0007).

Postgres DDL is transactional, so each migration file runs in one transaction together with
its schema_migrations row: a failure leaves nothing behind. A run holds an advisory lock, so
runners that start at the same time queue instead of racing.
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Any, Protocol

from traceblame_storage.migrations import POSTGRES, Migration
from traceblame_storage.runner import ChecksumMismatchError

__all__ = ["LOCK_KEY", "ChecksumMismatchError", "migrate", "status"]

# Session-scoped advisory lock key shared by every runner (the ASCII bytes of "tblm").
LOCK_KEY = 0x74626C6D
BOOKKEEPING = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version integer PRIMARY KEY, name text NOT NULL, phase text NOT NULL, "
    "sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
)
_APPLIED = "SELECT version, sha256 FROM schema_migrations ORDER BY version"
_RECORD = "INSERT INTO schema_migrations (version, name, phase, sha256) VALUES (%s, %s, %s, %s)"


class Result(Protocol):
    def fetchall(self) -> list[Any]: ...


class Connection(Protocol):
    """The part of a psycopg connection the runner uses (autocommit on)."""

    def execute(self, query: str, params: Sequence[Any] | None = None) -> Result: ...

    def transaction(self) -> AbstractContextManager[object]: ...


def _applied(conn: Connection) -> dict[int, str]:
    conn.execute(BOOKKEEPING)  # one statement: atomic on an autocommit connection
    return {int(v): str(s) for v, s in conn.execute(_APPLIED).fetchall()}


def status(conn: Connection, migrations: Sequence[Migration]) -> list[tuple[Migration, str]]:
    """Each migration with `applied`, `pending` or `changed`."""
    applied = _applied(conn)
    out: list[tuple[Migration, str]] = []
    for m in migrations:
        sha = applied.get(m.version)
        out.append((m, "pending" if sha is None else "applied" if sha == m.sha256 else "changed"))
    return out


def migrate(conn: Connection, migrations: Sequence[Migration]) -> list[Migration]:
    """Apply every pending migration in order; refuse if an applied one changed."""
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        applied = _applied(conn)
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
            with conn.transaction():
                for statement in m.statements(POSTGRES):
                    conn.execute(statement)
                conn.execute(_RECORD, (m.version, m.name, m.phase, m.sha256))
            done.append(m)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
