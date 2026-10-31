from collections.abc import Sequence
from typing import Any

import pytest

from traceblame_storage.migrations import Migration, parse
from traceblame_storage.runner import ChecksumMismatchError, DropGuardError, migrate, status


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


# What ClickHouse 26.8 answers when a DROP is over max_table_size_to_drop (error 359). The runner
# only recognises the code; every fact in its message comes from queries (#41).
_GUARD = (
    "HTTPDriver for http://localhost:8123 returned response code 500 )"
    "Code: 359. DB::Exception: Table or Partition in it_x.a was not dropped.\n"
    "(TABLE_SIZE_EXCEEDS_MAX_DROP_SIZE_LIMIT)"
)


class GuardClient(FakeClient):
    """Refuses DROP TABLE like a server whose max_table_size_to_drop is too low, and answers the
    runner's questions about it. `facts` makes those queries fail when it is None."""

    def __init__(self, refuse: bool = True, error: str = _GUARD) -> None:
        super().__init__()
        self.refuse = refuse
        self.error = error
        self.facts: dict[str, list[tuple[Any, ...]]] | None = {
            "system.parts": [("it_x", 73_400_320)],
            "system.server_settings": [("50000000000",)],
            "system.settings": [("0", 0)],
        }
        self.asked: list[str] = []

    def command(self, sql: str) -> None:
        if self.refuse and sql.startswith("DROP TABLE"):
            raise RuntimeError(self.error)
        super().command(sql)

    def query_rows(self, sql: str) -> list[tuple[Any, ...]]:
        if "schema_migrations" in sql:
            return super().query_rows(sql)
        self.asked.append(sql)
        if self.facts is None:
            raise RuntimeError("clickhouse is gone")
        return next(rows for table, rows in self.facts.items() if table in sql)


def _drops() -> list[Migration]:
    return [
        parse("0001_expand_a.sql", "CREATE TABLE IF NOT EXISTS a (x String) ENGINE = Null"),
        parse("0002_contract_drop_view.sql", "DROP VIEW IF EXISTS v"),
        parse("0003_contract_drop_a.sql", "DROP TABLE IF EXISTS a"),
    ]


def test_a_refused_drop_fails_with_a_clear_message_and_is_not_recorded() -> None:
    client = GuardClient()
    with pytest.raises(DropGuardError) as err:
        migrate(client, _drops())
    msg = str(err.value)
    assert "0003_contract_drop_a.sql" in msg
    assert "table it_x.a" in msg
    assert "size 73.40 MB, limit 50.00 GB" in msg
    assert "docs/runbooks/clickhouse-drop-guard.md" in msg
    assert "Code: 359" not in msg
    assert "DB::Exception" not in msg
    assert [r[0] for r in client.rows] == [1, 2], "the refused migration is not recorded"
    asked = " ".join(client.asked)
    assert "sum(bytes_on_disk)" in asked
    assert "active" in asked
    assert "max_table_size_to_drop" in asked


def test_a_session_setting_wins_over_the_server_setting() -> None:
    client = GuardClient()
    assert client.facts is not None
    client.facts["system.settings"] = [("1", 1)]
    with pytest.raises(DropGuardError, match=r"limit 1 B"):
        migrate(client, _drops())


def test_a_limit_of_zero_is_reported_as_unlimited() -> None:
    client = GuardClient()
    assert client.facts is not None
    client.facts["system.server_settings"] = [("0",)]
    with pytest.raises(DropGuardError, match=r"limit unlimited"):
        migrate(client, _drops())


@pytest.mark.parametrize(
    "garbled",
    [
        "Code: 359. \x00\xff <<<???>>> \u2603 no sizes, no table here",
        "TABLE_SIZE_EXCEEDS_MAX_DROP_SIZE_LIMIT",
        "Code: 359.\nReason:\n1. Size (many) is greater than the limit (few)\nWhatever",
    ],
)
def test_a_garbled_359_still_yields_the_clear_error(garbled: str) -> None:
    client = GuardClient(error=garbled)
    with pytest.raises(DropGuardError) as err:
        migrate(client, _drops())
    msg = str(err.value)
    assert "table it_x.a" in msg
    assert "size 73.40 MB, limit 50.00 GB" in msg
    assert "docs/runbooks/clickhouse-drop-guard.md" in msg
    for fragment in ("Code: 359", "Whatever", "few", "no table here", "\u2603"):
        assert fragment not in msg, "never the server text"


def test_when_the_facts_cannot_be_read_the_error_is_still_clear() -> None:
    client = GuardClient()
    client.facts = None
    with pytest.raises(DropGuardError) as err:
        migrate(client, _drops())
    msg = str(err.value)
    assert "0003_contract_drop_a.sql" in msg
    assert "table a" in msg
    assert "size/limit unavailable" in msg
    assert "docs/runbooks/clickhouse-drop-guard.md" in msg
    assert "Code: 359" not in msg
    assert "clickhouse is gone" not in msg
    assert [r[0] for r in client.rows] == [1, 2]


def test_a_rerun_after_the_limit_is_lifted_applies_only_the_refused_migration() -> None:
    client = GuardClient()
    with pytest.raises(DropGuardError):
        migrate(client, _drops())
    client.refuse = False
    assert [m.version for m in migrate(client, _drops())] == [3]
    assert [r[0] for r in client.rows] == [1, 2, 3]


def test_other_server_errors_are_not_rewritten() -> None:
    client = GuardClient(error="Code: 60. DB::Exception: Unknown table")
    with pytest.raises(RuntimeError, match="Code: 60"):
        migrate(client, _drops())
