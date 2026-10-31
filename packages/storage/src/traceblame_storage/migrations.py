"""Forward-only ClickHouse migrations: files, phases and the static rules (ADR-0004).

A migration is `NNNN_<phase>_<name>.sql` with one or more statements. Phases follow
expand -> migrate -> contract (CLAUDE.md hard rule 4):

- expand: create tables and views, add columns and indexes; never removes or changes;
- migrate: backfill (INSERT ... SELECT, ALTER ... UPDATE / MATERIALIZE);
- contract: drop what no running code reads any more.

Every statement is idempotent (IF [NOT] EXISTS), because ClickHouse DDL is not
transactional and a failed run is simply re-run. A migration that was applied, i.e. one
present on the base revision, never changes and is never removed.
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
PACKAGED_PATH = "packages/storage/src/traceblame_storage/migrations/clickhouse"
CONTRACT_PATH = "packages/storage/src/traceblame_storage/contract"


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

    def statements(self) -> list[str]:
        return split_statements(self.sql)


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


def packaged() -> list[Migration]:
    """The migrations shipped with this package."""
    return load_dir(resources.files("traceblame_storage") / "migrations" / "clickhouse")


def at_revision(ref: str, path: str = PACKAGED_PATH, cwd: Path | None = None) -> list[Migration]:
    """The migrations at a git revision; [] when the directory does not exist there."""
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


def split_statements(sql: str) -> list[str]:
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
                end += 2 if sql[end] == "\\" else 1
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


_IDENT = r"[A-Za-z_][A-Za-z0-9_.`]*"
_KINDS: dict[str, re.Pattern[str]] = {
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
}
_ALLOWED = {"expand": {"create", "add"}, "migrate": {"backfill"}, "contract": {"drop"}}


def _kind(statement: str) -> str | None:
    return next((k for k, p in _KINDS.items() if p.match(statement)), None)


def check(migrations: Sequence[Migration], base: Sequence[Migration] = ()) -> list[str]:
    """Every rule a migration set breaks; [] when it is valid against the base revision."""
    problems: list[str] = []
    for expected, m in enumerate(_sorted(migrations), start=1):
        if m.version != expected:
            problems.append(f"{m.filename}: expected version {expected}, versions are contiguous")
        statements = m.statements()
        if not statements:
            problems.append(f"{m.filename}: no statements")
        for s in statements:
            kind = _kind(s)
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
    current = {m.filename: m for m in migrations}
    for b in base:
        now = current.get(b.filename)
        if now is None:
            problems.append(f"{b.filename} was removed; applied migrations stay")
        elif now.sql != b.sql:
            problems.append(f"{b.filename} changed; applied migrations never change")
    return problems
