"""traceblame-faults embeds the eval_traces statements it runs; the copies equal the canonical
ones in tests/integration/ (#14). They move into the storage contract once eval_traces is on
main (code N must run on schema N-1)."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EMBEDDED = ROOT / "packages" / "faults" / "src" / "traceblame_faults" / "sql"

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("name", ["eval_traces_write.sql", "eval_traces_read.sql"])
def test_the_embedded_statement_is_the_canonical_one(name: str) -> None:
    assert (EMBEDDED / name).read_bytes() == (ROOT / "tests" / "integration" / name).read_bytes()


def test_nothing_else_is_embedded() -> None:
    assert sorted(p.name for p in EMBEDDED.iterdir()) == [
        "eval_traces_read.sql",
        "eval_traces_write.sql",
    ]
