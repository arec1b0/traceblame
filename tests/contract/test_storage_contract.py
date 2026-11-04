"""Migrations are forward-only for ClickHouse and Postgres (hard rule 4, ADR-0004, ADR-0007)."""

import os
from pathlib import Path

import pytest

from traceblame_storage.migrations import (
    CLICKHOUSE,
    POSTGRES,
    Dialect,
    at_revision,
    check,
    packaged,
)

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.contract


DIALECTS = [pytest.param(CLICKHOUSE, id="clickhouse"), pytest.param(POSTGRES, id="postgres")]


@pytest.mark.parametrize("dialect", DIALECTS)
def test_migrations_follow_the_phase_rules(dialect: Dialect) -> None:
    assert check(packaged(dialect), dialect=dialect) == []


@pytest.mark.parametrize("dialect", DIALECTS)
def test_applied_migrations_are_unchanged_against_the_base_revision(dialect: Dialect) -> None:
    """Every migration on CONTRACT_BASE_REF is still here, byte for byte."""
    ref = os.environ.get("CONTRACT_BASE_REF", "main")
    base = at_revision(ref, dialect, cwd=ROOT)
    if not base:
        pytest.skip(f"no {dialect.name} migrations at {ref} yet")
    assert check(packaged(dialect), base, dialect) == []
