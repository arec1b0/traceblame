from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from traceblame_storage.migrations import Migration, parse
from traceblame_storage.pg_runner import LOCK_KEY, ChecksumMismatchError, migrate, status


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeConnection:
    """Records statements and transaction boundaries; keeps schema_migrations in memory."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.log: list[str] = []
        self.rows: list[tuple[Any, ...]] = []
        self.in_transaction = False
        self.fail_on = fail_on
        self._pending: list[tuple[Any, ...]] = []

    def execute(self, query: str, params: Sequence[Any] | None = None) -> FakeResult:
        self.log.append(query if params is None else f"{query} {tuple(params)}")
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("boom")
        if query.startswith("INSERT INTO schema_migrations"):
            assert self.in_transaction, "the record must commit with the migration"
            assert params is not None
            self._pending.append(tuple(params))
        if query.startswith("SELECT version, sha256 FROM schema_migrations"):
            return FakeResult([(r[0], r[3]) for r in self.rows])
        return FakeResult([])

    @contextmanager
    def transaction(self) -> Generator[None]:
        self.log.append("BEGIN")
        self.in_transaction = True
        self._pending = []
        try:
            yield
        except BaseException:
            self.log.append("ROLLBACK")
            raise
        else:
            self.rows.extend(self._pending)
            self.log.append("COMMIT")
        finally:
            self.in_transaction = False


def _migrations() -> list[Migration]:
    return [
        parse("0001_expand_a.sql", "CREATE TABLE IF NOT EXISTS a (x text)"),
        parse(
            "0002_expand_b.sql",
            "CREATE TABLE IF NOT EXISTS b (x text);\nCREATE TABLE IF NOT EXISTS c (x text);",
        ),
    ]


def test_migrate_applies_each_file_in_one_transaction_and_records_it() -> None:
    conn = FakeConnection()
    applied = migrate(conn, _migrations())
    assert [m.version for m in applied] == [1, 2]
    assert conn.log.count("BEGIN") == 2
    assert conn.log.count("COMMIT") == 2
    assert [r[0] for r in conn.rows] == [1, 2]
    # the second file's two statements and its record sit between one BEGIN and one COMMIT
    begin = conn.log.index("BEGIN", conn.log.index("COMMIT"))
    second = conn.log[begin : conn.log.index("COMMIT", begin) + 1]
    assert second[0] == "BEGIN"
    assert second[-1] == "COMMIT"
    assert [line.split(" (")[0] for line in second[1:-1]] == [
        "CREATE TABLE IF NOT EXISTS b",
        "CREATE TABLE IF NOT EXISTS c",
        "INSERT INTO schema_migrations",
    ]

    again = FakeConnection()
    again.rows = list(conn.rows)
    assert migrate(again, _migrations()) == []
    assert "BEGIN" not in again.log


def test_migrate_holds_the_advisory_lock_for_the_whole_run() -> None:
    conn = FakeConnection()
    migrate(conn, _migrations())
    lock = f"SELECT pg_advisory_lock(%s) ({LOCK_KEY},)"
    unlock = f"SELECT pg_advisory_unlock(%s) ({LOCK_KEY},)"
    assert conn.log[0] == lock
    assert conn.log[-1] == unlock
    assert conn.log.index("BEGIN") > 0


def test_a_failed_statement_rolls_the_migration_back_and_releases_the_lock() -> None:
    conn = FakeConnection(fail_on="CREATE TABLE IF NOT EXISTS c")
    with pytest.raises(RuntimeError, match="boom"):
        migrate(conn, _migrations())
    assert [r[0] for r in conn.rows] == [1]  # the first file stays, the second leaves nothing
    assert "ROLLBACK" in conn.log
    assert conn.log[-1].startswith("SELECT pg_advisory_unlock")


def test_migrate_refuses_a_changed_applied_migration() -> None:
    conn = FakeConnection()
    migrate(conn, _migrations())
    changed = [parse("0001_expand_a.sql", "CREATE TABLE IF NOT EXISTS a (y text)")]
    with pytest.raises(ChecksumMismatchError, match=r"0001_expand_a\.sql"):
        migrate(conn, changed)
    assert conn.log[-1].startswith("SELECT pg_advisory_unlock")


def test_status_reports_applied_pending_and_changed() -> None:
    conn = FakeConnection()
    migrate(conn, _migrations()[:1])
    rows = status(conn, _migrations())
    assert [(m.version, s) for m, s in rows] == [(1, "applied"), (2, "pending")]
    changed = [parse("0001_expand_a.sql", "SELECT 1"), _migrations()[1]]
    assert status(conn, changed)[0][1] == "changed"
