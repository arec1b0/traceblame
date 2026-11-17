"""traceblame-faults embeds the eval statements it runs (eval_traces, #14; eval_exclusions and
the eval reads, #70); the copies equal the canonical ones in tests/integration/. They move into
the storage contract once their tables are on main (code N must run on schema N-1)."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EMBEDDED = ROOT / "packages" / "faults" / "src" / "traceblame_faults" / "sql"

pytestmark = pytest.mark.contract


NAMES = [
    "eval_dataset_read.sql",
    "eval_exclusions_read.sql",
    "eval_exclusions_write.sql",
    "eval_injections_by_id_read.sql",
    "eval_run_traces_read.sql",
    "eval_traces_read.sql",
    "eval_traces_write.sql",
]


@pytest.mark.parametrize("name", NAMES)
def test_the_embedded_statement_is_the_canonical_one(name: str) -> None:
    assert (EMBEDDED / name).read_bytes() == (ROOT / "tests" / "integration" / name).read_bytes()


def test_nothing_else_is_embedded() -> None:
    assert sorted(p.name for p in EMBEDDED.iterdir()) == NAMES


def test_the_label_read_joins_on_the_trace_id_as_well_as_the_injection_id() -> None:
    """Never by injection id alone: an abandoned trace's spans carry the same id (#70)."""
    sql = (EMBEDDED / "eval_dataset_read.sql").read_text(encoding="utf-8")
    assert "ON i.trace_id = e.trace_id AND i.injection_id = e.injection_id" in sql
    assert "GROUP BY trace_id, injection_id" in sql
