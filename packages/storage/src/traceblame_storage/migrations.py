"""Forward-only migrations: files, phases and the static rules (ADR-0004, ADR-0007).

A migration is `NNNN_<phase>_<name>.sql` with one or more statements. Phases follow
expand -> migrate -> contract (CLAUDE.md hard rule 4):

- expand: create tables and views, add columns and indexes; never removes or changes;
- migrate: backfill (INSERT ... SELECT, ALTER ... UPDATE / MATERIALIZE);
- contract: drop what no running code reads any more.

Every statement is idempotent (IF [NOT] EXISTS), because ClickHouse DDL is not
transactional and a failed run is simply re-run; Postgres applies the same files, one
transaction each. A migration that was applied, i.e. one present on the base revision, never
changes and is never removed.
"""

import hashlib
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

PHASES = ("expand", "migrate", "contract")
_FILENAME = re.compile(r"^(\d{4})_(expand|migrate|contract)_([a-z0-9_]+)\.sql$")
# Repository path of the packaged migrations, for reading them at a git revision.
MIGRATIONS_ROOT = "packages/storage/src/traceblame_storage/migrations"
PACKAGED_PATH = f"{MIGRATIONS_ROOT}/clickhouse"
CONTRACT_PATH = "packages/storage/src/traceblame_storage/contract"


_IDENT = r"[A-Za-z_][A-Za-z0-9_.`]*"


@dataclass(frozen=True)
class Dialect:
    """One database's migrations: the directory and what each phase may contain."""

    name: str
    directory: str
    kinds: dict[str, re.Pattern[str]]
    # ClickHouse strings take backslash escapes; Postgres standard strings do not.
    backslash_escapes: bool

    @property
    def path(self) -> str:
        return f"{MIGRATIONS_ROOT}/{self.directory}"


CLICKHOUSE = Dialect(
    name="clickhouse",
    directory="clickhouse",
    kinds={
        "create": re.compile(
            r"^CREATE (TABLE|MATERIALIZED VIEW|VIEW|DICTIONARY) IF NOT EXISTS ", re.IGNORECASE
        ),
        "add": re.compile(
            rf"^ALTER TABLE {_IDENT} ADD (COLUMN|INDEX|PROJECTION) IF NOT EXISTS ", re.IGNORECASE
        ),
        "backfill": re.compile(
            rf"^(INSERT INTO {_IDENT}\b.*\bSELECT\b"
            rf"|ALTER TABLE {_IDENT} (UPDATE|MATERIALIZE (COLUMN|INDEX|PROJECTION)) )",
            re.IGNORECASE,
        ),
        "drop": re.compile(
            rf"^(DROP (TABLE|VIEW|DICTIONARY) IF EXISTS "
            rf"|ALTER TABLE {_IDENT} DROP (COLUMN|INDEX|PROJECTION) IF EXISTS )",
            re.IGNORECASE,
        ),
    },
    backslash_escapes=True,
)

# ADR-0007. No CONCURRENTLY (it cannot run in the migration's transaction), no constraints
# added to an existing table (ADD CONSTRAINT has no IF NOT EXISTS): a constraint belongs in
# the CREATE TABLE.
POSTGRES = Dialect(
    name="postgres",
    directory="postgres",
    kinds={
        "create": re.compile(r"^CREATE ((UNIQUE )?INDEX|TABLE|VIEW) IF NOT EXISTS ", re.IGNORECASE),
        "add": re.compile(rf"^ALTER TABLE {_IDENT} ADD COLUMN IF NOT EXISTS ", re.IGNORECASE),
        "backfill": re.compile(
            # S608 below: a statement pattern for the phase check, not a query.
            rf"^(INSERT INTO {_IDENT}\b.*\bSELECT\b|UPDATE {_IDENT} SET )",  # noqa: S608
            re.IGNORECASE,
        ),
        "drop": re.compile(
            rf"^(DROP (TABLE|INDEX|VIEW) IF EXISTS "
            rf"|ALTER TABLE {_IDENT} DROP COLUMN IF EXISTS )",
            re.IGNORECASE,
        ),
    },
    backslash_escapes=False,
)
_ALLOWED = {"expand": {"create", "add"}, "migrate": {"backfill"}, "contract": {"drop"}}

# The clauses of a ClickHouse expand ALTER. The statement pattern sees only the first clause, so
# every clause is checked: additions, plus MODIFY ORDER BY after at least one ADD COLUMN.
# ClickHouse extends a sorting key only with columns added in the same ALTER (and rejects an
# existing column itself), which keeps the change metadata-only and the old rows in order.
_ALTER_HEAD = re.compile(rf"^ALTER TABLE {_IDENT} ", re.IGNORECASE)
_ADD_CLAUSE = re.compile(r"^ADD (COLUMN|INDEX|PROJECTION) IF NOT EXISTS ", re.IGNORECASE)
_ADD_COLUMN_CLAUSE = re.compile(r"^ADD COLUMN IF NOT EXISTS ", re.IGNORECASE)
_ORDER_BY_CLAUSE = re.compile(r"^MODIFY ORDER BY ", re.IGNORECASE)


def _clauses(statement: str) -> list[str]:
    """The comma-separated clauses of an ALTER, ignoring commas in parentheses and quotes."""
    head = _ALTER_HEAD.match(statement)
    body = statement[head.end() :] if head else statement
    clauses: list[str] = []
    current: list[str] = []
    depth, quote = 0, ""
    for c in body:
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            clauses.append("".join(current).strip())
            current = []
            continue
        current.append(c)
    clauses.append("".join(current).strip())
    return clauses


def _expand_alter_problems(statement: str) -> list[str]:
    problems: list[str] = []
    added_column = False
    clauses = _clauses(statement)
    for i, clause in enumerate(clauses):
        if _ADD_CLAUSE.match(clause):
            added_column = added_column or bool(_ADD_COLUMN_CLAUSE.match(clause))
            continue
        if _ORDER_BY_CLAUSE.match(clause) and added_column and i == len(clauses) - 1:
            continue
        problems.append(f"clause not allowed in an expand ALTER: {clause[:60]}")
    return problems


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    phase: str
    name: str
    sql: str

    @property
    def filename(self) -> str:
        return f"{self.version:04d}_{self.phase}_{self.name}.sql"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    def statements(self, dialect: Dialect | None = None) -> list[str]:
        return split_statements(
            self.sql, backslash_escapes=True if dialect is None else dialect.backslash_escapes
        )


def parse(filename: str, sql: str) -> Migration:
    m = _FILENAME.fullmatch(filename)
    if m is None:
        raise MigrationError(f"{filename}: expected NNNN_<expand|migrate|contract>_<name>.sql")
    return Migration(int(m.group(1)), m.group(2), m.group(3), sql)


def _sorted(migrations: Iterable[Migration]) -> list[Migration]:
    return sorted(migrations, key=lambda m: m.version)


def load_dir(path: Path | Traversable) -> list[Migration]:
    return _sorted(
        parse(f.name, f.read_text(encoding="utf-8"))
        for f in path.iterdir()
        if f.name.endswith(".sql")
    )


def packaged(dialect: Dialect = CLICKHOUSE) -> list[Migration]:
    """The migrations shipped with this package."""
    return load_dir(resources.files("traceblame_storage") / "migrations" / dialect.directory)


def at_revision(
    ref: str, dialect: Dialect = CLICKHOUSE, cwd: Path | None = None
) -> list[Migration]:
    """The migrations at a git revision; [] when the directory does not exist there."""
    path = dialect.path
    files = git_text(["ls-tree", "--name-only", f"{ref}:{path}"], cwd)
    if files is None:
        return []
    return _sorted(
        parse(name, git_text(["show", f"{ref}:{path}/{name}"], cwd) or "")
        for name in files.splitlines()
        if name.endswith(".sql")
    )


def git_text(args: Sequence[str], cwd: Path | None = None) -> str | None:
    git = shutil.which("git")
    if git is None:
        raise MigrationError("git is not available")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def split_statements(sql: str, *, backslash_escapes: bool = True) -> list[str]:
    """Statements separated by ';', ignoring comments and quoted text."""
    statements: list[str] = []
    current: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            i = sql.find("\n", i)
            i = n if i < 0 else i
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c in "'\"`":
            end = i + 1
            while end < n and sql[end] != c:
                end += 2 if backslash_escapes and sql[end] == "\\" else 1
            current.append(sql[i : end + 1])
            i = end + 1
            continue
        if c == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(c)
        i += 1
    statements.append("".join(current))
    return [" ".join(s.split()) for s in statements if s.strip()]


def _kind(statement: str, dialect: Dialect) -> str | None:
    return next((k for k, p in dialect.kinds.items() if p.match(statement)), None)


def check(
    migrations: Sequence[Migration],
    base: Sequence[Migration] = (),
    dialect: Dialect = CLICKHOUSE,
) -> list[str]:
    """Every rule a migration set breaks; [] when it is valid against the base revision."""
    problems: list[str] = []
    for expected, m in enumerate(_sorted(migrations), start=1):
        if m.version != expected:
            problems.append(f"{m.filename}: expected version {expected}, versions are contiguous")
        statements = m.statements(dialect)
        if not statements:
            problems.append(f"{m.filename}: no statements")
        for s in statements:
            kind = _kind(s, dialect)
            if kind not in _ALLOWED[m.phase]:
                article = "an" if m.phase == "expand" else "a"
                hint = (
                    " (CREATE/DROP need IF [NOT] EXISTS)"
                    if s.upper().startswith(("CREATE", "DROP"))
                    else ""
                )
                problems.append(
                    f"{m.filename}: statement not allowed in {article} {m.phase} migration"
                    f"{hint}: {s[:80]}"
                )
                if s.upper().startswith("CREATE") and "IF NOT EXISTS" not in s.upper():
                    problems.append(f"{m.filename}: CREATE without IF NOT EXISTS")
            elif m.phase == "expand" and kind == "add" and dialect is CLICKHOUSE:
                problems.extend(f"{m.filename}: {p}" for p in _expand_alter_problems(s))
    current = {m.filename: m for m in migrations}
    for b in base:
        now = current.get(b.filename)
        if now is None:
            problems.append(f"{b.filename} was removed; applied migrations stay")
        elif now.sql != b.sql:
            problems.append(f"{b.filename} changed; applied migrations never change")
    return problems
