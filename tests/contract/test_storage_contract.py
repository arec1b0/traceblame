"""ClickHouse migrations are forward-only (CLAUDE.md hard rule 4, ADR-0004)."""

import os
from pathlib import Path

import pytest

from traceblame_storage.migrations import at_revision, check, packaged

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.contract


def test_migrations_follow_the_phase_rules() -> None:
    assert check(packaged()) == []


def test_applied_migrations_are_unchanged_against_the_base_revision() -> None:
    """Every migration on CONTRACT_BASE_REF is still here, byte for byte."""
    ref = os.environ.get("CONTRACT_BASE_REF", "main")
    base = at_revision(ref, cwd=ROOT)
    if not base:
        pytest.skip(f"no migrations at {ref} yet")
    assert check(packaged(), base) == []
