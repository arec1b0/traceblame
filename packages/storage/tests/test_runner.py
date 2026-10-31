from collections.abc import Sequence
from typing import Any

import pytest

from traceblame_storage.migrations import Migration, parse
from traceblame_storage.runner import ChecksumMismatchError, migrate, status


class FakeClient:
    """Records DDL and keeps schema_migrations rows in memory."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.rows: list[tuple[Any, ...]] = []

    def command(self, sql: str) -> None:
        self.commands.append(sql)

    def query_rows(self, sql: str) -> list[tuple[Any, ...]]:
        assert "schema_migrations" in sql
        return [(r[0], r[3]) for r in self.rows]

    def insert(
        self, table: str, rows: Sequence[Sequence[Any]], column_names: Sequence[str]
    ) -> None:
        assert table == "schema_migrations"
        assert list(column_names) == ["version", "name", "phase", "sha256"]
        self.rows.extend(tuple(r) for r in rows)


def _migrations() -> list[Migration]:
    return [
        parse("0001_expand_a.sql", "CREATE TABLE IF NOT EXISTS a (x String) ENGINE = Null"),
        parse(
            "0002_expand_b.sql",
            "CREATE TABLE IF NOT EXISTS b (x String) ENGINE = Null;\n"
            "CREATE TABLE IF NOT EXISTS c (x String) ENGINE = Null;",
        ),
    ]


def test_migrate_applies_each_statement_once_and_records_it() -> None:
    client = FakeClient()
    applied = migrate(client, _migrations())
    assert [m.version for m in applied] == [1, 2]
    ddl = [c for c in client.commands if "schema_migrations" not in c]
    assert len(ddl) == 3
    assert [r[0] for r in client.rows] == [1, 2]

    assert migrate(client, _migrations()) == []
    assert len([c for c in client.commands if "schema_migrations" not in c]) == 3


def test_migrate_refuses_a_changed_applied_migration() -> None:
    client = FakeClient()
    migrate(client, _migrations())
    changed = [parse("0001_expand_a.sql", "CREATE TABLE IF NOT EXISTS a (y String) ENGINE = Null")]
    with pytest.raises(ChecksumMismatchError, match=r"0001_expand_a\.sql"):
        migrate(client, changed)


def test_status_reports_applied_pending_and_changed() -> None:
    client = FakeClient()
    migrate(client, _migrations()[:1])
    rows = status(client, _migrations())
    assert [(m.version, s) for m, s in rows] == [(1, "applied"), (2, "pending")]
    changed = [parse("0001_expand_a.sql", "SELECT 1"), _migrations()[1]]
    assert status(client, changed)[0][1] == "changed"
