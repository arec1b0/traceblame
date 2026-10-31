"""The profile, its generated JSON Schema and the pinned semconv snapshot agree (ADR-0001)."""

import hashlib
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
from traceblame_contracts.vectors import (
    VectorFile,
    load_registry,
    load_registry_from_git,
    registry_problems,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = ROOT / "schemas" / "profile" / "aegis-telemetry.v1.yaml"
VECTORS_DIR = ROOT / "schemas" / "profile" / "vectors" / "v1"
ARGS_HASH = "aegis.tool.args_hash"

pytestmark = pytest.mark.contract


def test_committed_schema_is_generated_from_the_profile(profile: Profile) -> None:
    committed = schema_path_for(PROFILE_PATH).read_text(encoding="utf-8")
    assert committed == render_json_schema(profile), "run `traceblame-contracts gen-schema`"


def test_the_normalizer_embeds_the_same_profile() -> None:
    mirror = ROOT / "collector" / "processor" / "normalizer" / "profiles" / PROFILE_PATH.name
    assert mirror.read_bytes() == PROFILE_PATH.read_bytes(), "run `traceblame-contracts gen-schema`"


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
    """Within one major only minor changes (ADR-0001 section 2); base: CONTRACT_BASE_REF.

    Golden vectors are read at the same two revisions: a reword is minor only against
    vectors that already existed at the base (ADR-0002 section 5).
    """
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
    base_vectors = load_registry_from_git(ref, VECTORS_DIR.relative_to(ROOT).as_posix(), cwd=ROOT)
    assert (
        breaking_within_major(
            base, profile, old_vectors=base_vectors, new_vectors=load_registry(VECTORS_DIR)
        )
        == []
    )


def test_every_registered_vector_belongs_to_the_profile(profile: Profile) -> None:
    assert registry_problems(profile, load_registry(VECTORS_DIR)) == []


def test_args_hash_has_registered_vectors() -> None:
    registry = load_registry(VECTORS_DIR)
    assert ARGS_HASH in registry, "aegis.tool.args_hash needs golden vectors (ADR-0002 section 5)"


def _reference_args_hash(file: VectorFile, vector_id: str) -> str:
    """SHA-256 over the canonical form (raw UTF-8 input when canonical is null), 16 hex chars."""
    vector = next(v for v in file.vectors if v.id == vector_id)
    material = vector.input if vector.canonical is None else vector.canonical
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def test_args_hash_vectors_agree_with_an_independent_reference() -> None:
    """The registry is self-consistent without the Go normalizer: expected = sha256(canonical)."""
    file = load_registry(VECTORS_DIR)[ARGS_HASH]
    wrong = [v.id for v in file.vectors if _reference_args_hash(file, v.id) != v.expected]
    assert wrong == []


def test_args_hash_vectors_cover_the_required_cases() -> None:
    file = load_registry(VECTORS_DIR)[ARGS_HASH]
    ids = {v.id for v in file.vectors}
    raw = [v for v in file.vectors if v.canonical is None]
    assert len(file.vectors) >= 20
    assert {"rfc8785-3-2-3-sample", "rfc8785-3-2-3-utf16-property-order"} <= ids
    assert any("key-order" in i for i in ids)
    assert any("number" in i for i in ids)
    assert any("unicode" in i for i in ids)
    assert raw, "the raw-UTF-8 fallback needs vectors"
    assert any(not v.input.isascii() for v in raw), "a non-ASCII raw-fallback case"
