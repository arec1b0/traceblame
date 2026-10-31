import copy
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from traceblame_contracts.profile import Profile
from traceblame_contracts.vectors import (
    Vector,
    VectorFile,
    load_registry,
    load_registry_from_git,
    registry_problems,
)

ATTRIBUTE = "aegis.span.kind"  # the carrier in the minimal profile


def _vector(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": "key-order",
        "input": '{"b": 1, "a": 2}',
        "canonical": '{"a":2,"b":1}',
        "expected": hashlib.sha256(b'{"a":2,"b":1}').hexdigest()[:16],
    }
    return doc | overrides


def _file(*vectors: dict[str, Any], version: int = 1, attribute: str = ATTRIBUTE) -> dict[str, Any]:
    return {
        "attribute": attribute,
        "algorithm_version": version,
        "vectors": list(vectors) or [_vector()],
    }


def _write(directory: Path, doc: dict[str, Any], name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or f"{doc['attribute']}.json")
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _versioned_profile(minimal_profile: dict[str, Any], version: int | None = 1) -> Profile:
    doc = copy.deepcopy(minimal_profile)
    source = next(a for a in doc["attributes"] if a["name"] == ATTRIBUTE)["source"]
    source["algorithm"] = "sha256 of the key-sorted arguments"
    if version is not None:
        source["algorithm_version"] = version
    return Profile.model_validate(doc)


# The model.


def test_a_vector_file_parses() -> None:
    parsed = VectorFile.model_validate(_file(_vector(note="sorted")))
    assert parsed.vectors[0].note == "sorted"
    assert parsed.vectors[0].canonical == '{"a":2,"b":1}'


def test_canonical_may_be_null_for_the_raw_fallback() -> None:
    parsed = VectorFile.model_validate(_file(_vector(id="raw", canonical=None)))
    assert parsed.vectors[0].canonical is None


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        VectorFile.model_validate(_file(_vector(), _vector(input="{}")))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        VectorFile.model_validate(_file(_vector(extra="x")))
    with pytest.raises(ValidationError):
        VectorFile.model_validate(_file() | {"owner": "me"})


def test_a_file_without_vectors_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VectorFile.model_validate(_file() | {"vectors": []})


@pytest.mark.parametrize("version", [0, -1])
def test_algorithm_version_is_positive(version: int) -> None:
    with pytest.raises(ValidationError):
        VectorFile.model_validate(_file(version=version))


@pytest.mark.parametrize("vector_id", ["", "Key Order", "-x", "a/b"])
def test_vector_ids_are_slugs(vector_id: str) -> None:
    with pytest.raises(ValidationError):
        Vector.model_validate(_vector(id=vector_id))


# Loading from a directory.


def test_load_registry_keys_files_by_attribute(tmp_path: Path) -> None:
    _write(tmp_path, _file())
    registry = load_registry(tmp_path)
    assert list(registry) == [ATTRIBUTE]
    assert registry[ATTRIBUTE].vectors[0].id == "key-order"


def test_load_registry_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path / "absent") == {}


def test_load_registry_ignores_other_files(tmp_path: Path) -> None:
    _write(tmp_path, _file())
    (tmp_path / "README.md").write_text("docs\n", encoding="utf-8")
    assert list(load_registry(tmp_path)) == [ATTRIBUTE]


def test_file_name_must_match_the_attribute(tmp_path: Path) -> None:
    _write(tmp_path, _file(), name="aegis.other.json")
    with pytest.raises(ValueError, match=r"aegis\.other\.json"):
        load_registry(tmp_path)


# Loading from a git revision.


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    root = tmp_path / "repo"
    root.mkdir()
    for argv in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
        ["config", "commit.gpgsign", "false"],
    ):
        subprocess.run([git, *argv], cwd=root, check=True)  # noqa: S603 - fixed argv
    return root


def _commit(root: Path, message: str) -> None:
    git = shutil.which("git") or "git"
    subprocess.run([git, "add", "-A"], cwd=root, check=True)  # noqa: S603
    subprocess.run([git, "commit", "-q", "-m", message], cwd=root, check=True)  # noqa: S603


def test_load_registry_from_git_reads_the_revision_not_the_working_tree(repo: Path) -> None:
    directory = "vectors/v1"
    _write(repo / directory, _file())
    _commit(repo, "vectors")
    _write(repo / directory, _file(_vector(expected="0" * 16)))  # uncommitted edit
    registry = load_registry_from_git("HEAD", directory, cwd=repo)
    assert registry[ATTRIBUTE].vectors[0].expected != "0" * 16


def test_load_registry_from_git_without_the_directory_is_empty(repo: Path) -> None:
    (repo / "x").write_text("x\n", encoding="utf-8")
    _commit(repo, "init")
    assert load_registry_from_git("HEAD", "vectors/v1", cwd=repo) == {}


def test_load_registry_from_git_with_an_unknown_ref_fails(repo: Path) -> None:
    (repo / "x").write_text("x\n", encoding="utf-8")
    _commit(repo, "init")
    with pytest.raises(RuntimeError, match="no-such-ref"):
        load_registry_from_git("no-such-ref", "vectors/v1", cwd=repo)


# Registry against a profile.


def test_a_consistent_registry_has_no_problems(minimal_profile: dict[str, Any]) -> None:
    profile = _versioned_profile(minimal_profile)
    assert registry_problems(profile, {ATTRIBUTE: VectorFile.model_validate(_file())}) == []


def test_vectors_for_an_unknown_attribute_are_a_problem(minimal_profile: dict[str, Any]) -> None:
    profile = _versioned_profile(minimal_profile)
    file = VectorFile.model_validate(_file(attribute="aegis.nope"))
    (problem,) = registry_problems(profile, {"aegis.nope": file})
    assert "aegis.nope" in problem


def test_vectors_for_an_attribute_without_an_algorithm_version_are_a_problem(
    minimal_profile: dict[str, Any],
) -> None:
    profile = _versioned_profile(minimal_profile, version=None)
    (problem,) = registry_problems(profile, {ATTRIBUTE: VectorFile.model_validate(_file())})
    assert "algorithm_version" in problem


def test_a_version_that_differs_from_the_profile_is_a_problem(
    minimal_profile: dict[str, Any],
) -> None:
    profile = _versioned_profile(minimal_profile, version=1)
    (problem,) = registry_problems(
        profile, {ATTRIBUTE: VectorFile.model_validate(_file(version=2))}
    )
    assert "2" in problem
    assert "1" in problem
