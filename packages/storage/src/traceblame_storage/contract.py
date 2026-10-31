"""The storage contract: the statements TraceBlame code runs against ClickHouse.

Each statement is one file in contract/. Writes run before reads (`*_write.sql` sort first
by table, then `*_read.sql`), so a run against a fresh schema exercises both.
"""

from importlib import resources
from pathlib import Path

from traceblame_storage.migrations import CONTRACT_PATH, git_text, split_statements


def _ordered(files: dict[str, str]) -> list[tuple[str, str]]:
    def key(name: str) -> tuple[int, str]:
        return (0 if name.endswith("_write.sql") else 1, name)

    out: list[tuple[str, str]] = []
    for name in sorted(files, key=key):
        statements = split_statements(files[name])
        if len(statements) != 1:
            raise ValueError(f"{name}: one statement per contract file")
        out.append((name, statements[0]))
    return out


def packaged() -> list[tuple[str, str]]:
    root = resources.files("traceblame_storage") / "contract"
    return _ordered(
        {f.name: f.read_text(encoding="utf-8") for f in root.iterdir() if f.name.endswith(".sql")}
    )


def at_revision(ref: str, cwd: Path | None = None) -> list[tuple[str, str]]:
    """The contract statements at a git revision; [] when there are none there."""
    names = git_text(["ls-tree", "--name-only", f"{ref}:{CONTRACT_PATH}"], cwd)
    if names is None:
        return []
    return _ordered(
        {
            n: git_text(["show", f"{ref}:{CONTRACT_PATH}/{n}"], cwd) or ""
            for n in names.splitlines()
            if n.endswith(".sql")
        }
    )
