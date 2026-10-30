"""The profile, its generated JSON Schema and the pinned semconv snapshot agree (ADR-0001)."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from traceblame_contracts.cli import schema_path_for
from traceblame_contracts.compat import breaking_within_major
from traceblame_contracts.jsonschema_gen import render_json_schema
from traceblame_contracts.profile import OPERATION_ATTRIBUTE, Profile
from traceblame_contracts.semconv import Snapshot

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "schemas" / "profile" / "aegis-telemetry.v1.yaml"

pytestmark = pytest.mark.contract


def test_committed_schema_is_generated_from_the_profile(profile: Profile) -> None:
    committed = schema_path_for(PROFILE_PATH).read_text(encoding="utf-8")
    assert committed == render_json_schema(profile), "run `traceblame-contracts gen-schema`"


def test_schema_is_valid_draft_2020_12(committed_schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(committed_schema)


def test_snapshot_is_the_pinned_release_and_intact(profile: Profile, snapshot: Snapshot) -> None:
    assert snapshot.source.repository == profile.semconv.repository
    assert snapshot.source.version == profile.semconv.version
    assert snapshot.source.commit == profile.semconv.commit
    assert snapshot.source.schema_url == profile.semconv.schema_url
    assert snapshot.checksum_mismatches() == []


def test_semconv_attributes_exist_undeprecated_with_the_same_type(
    profile: Profile, snapshot: Snapshot
) -> None:
    problems: list[str] = []
    for attr in profile.attributes:
        if attr.is_aegis:
            continue
        upstream = snapshot.attributes.get(attr.name)
        if upstream is None:
            problems.append(f"{attr.name}: not in semconv {profile.semconv.version}")
        elif upstream.deprecated:
            problems.append(f"{attr.name}: deprecated in semconv {profile.semconv.version}")
        elif upstream.type != attr.type:
            problems.append(f"{attr.name}: profile {attr.type}, semconv {upstream.type}")
    assert problems == []


def test_span_kind_operations_are_well_known_values(profile: Profile, snapshot: Snapshot) -> None:
    known = set(snapshot.attributes[OPERATION_ATTRIBUTE].members)
    unknown = sorted({op for kind in profile.span_kinds.values() for op in kind.operations} - known)
    assert unknown == []


def test_forbidden_attributes_are_semconv_names(profile: Profile, snapshot: Snapshot) -> None:
    assert [f.name for f in profile.forbidden if f.name not in snapshot.attributes] == []


def test_no_breaking_change_against_the_base_revision(profile: Profile) -> None:
    """Within one major only minor changes (ADR-0001 section 2); base: CONTRACT_BASE_REF."""
    ref = os.environ.get("CONTRACT_BASE_REF", "main")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    path = PROFILE_PATH.relative_to(ROOT).as_posix()
    shown = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if shown.returncode != 0:
        pytest.skip(f"no baseline profile at {ref}:{path}")
    base = Profile.model_validate(yaml.safe_load(shown.stdout))
    assert breaking_within_major(base, profile) == []
