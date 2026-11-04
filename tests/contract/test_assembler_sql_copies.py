"""The trace-assembler embeds the SQL it runs; the copies equal their sources (#35)."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.contract


def test_the_embedded_statements_are_the_contract_statements() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(ROOT / "scripts" / "sync-assembler-sql.sh"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
