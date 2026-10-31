from pathlib import Path

import pytest

from traceblame_storage.migrations import (
    Migration,
    MigrationError,
    check,
    load_dir,
    packaged,
    parse,
    split_statements,
)


def test_parse_reads_version_phase_and_name() -> None:
    m = parse("0007_expand_spans_add_x.sql", "SELECT 1")
    assert (m.version, m.phase, m.name) == (7, "expand", "spans_add_x")
    assert m.filename == "0007_expand_spans_add_x.sql"
    assert len(m.sha256) == 64


@pytest.mark.parametrize(
    "name", ["7_expand_x.sql", "0007_grow_x.sql", "0007_expand_X.sql", "0007_expand_x.txt"]
)
def test_parse_rejects_bad_names(name: str) -> None:
    with pytest.raises(MigrationError):
        parse(name, "SELECT 1")


def test_split_statements_ignores_comments_and_quoted_semicolons() -> None:
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE IF NOT EXISTS a (x String DEFAULT 'a;b') ENGINE = Null;

    /* block; comment */
    CREATE TABLE IF NOT EXISTS b (y String) ENGINE = Null
    """
    assert split_statements(sql) == [
        "CREATE TABLE IF NOT EXISTS a (x String DEFAULT 'a;b') ENGINE = Null",
        "CREATE TABLE IF NOT EXISTS b (y String) ENGINE = Null",
    ]


def _m(version: int, phase: str, sql: str, name: str = "x") -> Migration:
    return parse(f"{version:04d}_{phase}_{name}.sql", sql)


def test_check_accepts_the_packaged_migrations() -> None:
    assert check(packaged()) == []


@pytest.mark.parametrize(
    ("migration", "problem"),
    [
        (_m(1, "expand", "CREATE TABLE t (x String) ENGINE = Null"), "IF NOT EXISTS"),
        (_m(1, "expand", "DROP TABLE IF EXISTS t"), "not allowed in an expand"),
        (_m(1, "expand", "ALTER TABLE t RENAME COLUMN a TO b"), "not allowed in an expand"),
        (_m(1, "expand", "ALTER TABLE t MODIFY COLUMN a UInt8"), "not allowed in an expand"),
        (_m(1, "migrate", "DROP TABLE IF EXISTS t"), "not allowed in a migrate"),
        (_m(1, "contract", "DROP TABLE t"), "not allowed in a contract"),
        (_m(1, "contract", "CREATE TABLE IF NOT EXISTS t (x String) ENGINE = Null"), "contract"),
        (_m(1, "expand", ""), "no statements"),
    ],
)
def test_check_enforces_the_phase_rules(migration: Migration, problem: str) -> None:
    problems = check([migration])
    assert any(problem in p for p in problems), problems


def test_check_allows_each_phase_its_statements() -> None:
    migrations = [
        _m(1, "expand", "CREATE TABLE IF NOT EXISTS t (a String) ENGINE = MergeTree ORDER BY a"),
        _m(2, "expand", "ALTER TABLE t ADD COLUMN IF NOT EXISTS b String"),
        _m(3, "migrate", "ALTER TABLE t UPDATE b = a WHERE b = ''"),
        _m(4, "migrate", "INSERT INTO t (a, b) SELECT a, b FROM t2"),
        _m(5, "contract", "ALTER TABLE t DROP COLUMN IF EXISTS a"),
    ]
    assert check(migrations) == []


def test_check_needs_contiguous_versions() -> None:
    sql = "CREATE TABLE IF NOT EXISTS t (a String) ENGINE = Null"
    assert any("version 2" in p for p in check([_m(1, "expand", sql), _m(3, "expand", sql)]))


def test_check_keeps_applied_migrations_byte_identical() -> None:
    sql = "CREATE TABLE IF NOT EXISTS t (a String) ENGINE = Null"
    base = [_m(1, "expand", sql), _m(2, "expand", sql, "y")]
    edited = [_m(1, "expand", sql + " -- edited"), _m(2, "expand", sql, "y")]
    assert any("0001_expand_x.sql changed" in p for p in check(edited, base))
    assert any("0002_expand_y.sql was removed" in p for p in check([base[0]], base))
    assert check([*base, _m(3, "expand", sql, "z")], base) == []


def test_load_dir_sorts_by_version(tmp_path: Path) -> None:
    (tmp_path / "0002_expand_b.sql").write_text("SELECT 2", "utf-8")
    (tmp_path / "0001_expand_a.sql").write_text("SELECT 1", "utf-8")
    (tmp_path / "README.md").write_text("not a migration", "utf-8")
    assert [m.version for m in load_dir(tmp_path)] == [1, 2]
