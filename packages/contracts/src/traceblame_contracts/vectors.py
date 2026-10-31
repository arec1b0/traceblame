"""Golden vectors of profile algorithms (ADR-0002, section 5).

A vector pins the output of an attribute's `source.algorithm` for one input. Vectors live in
`schemas/profile/vectors/v<major>/<attribute>.json`, one file per attribute (README there).
Every implementation of the algorithm runs all of them as a contract test; the compat checker
only reads them at two revisions and verifies they were not edited to fit a change.
"""

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from traceblame_contracts.profile import AegisSource, Profile

_VECTOR_ID = r"^[a-z0-9][a-z0-9._-]*$"
_SUFFIX = ".json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vector(_Model):
    """One input and the output every implementation must produce for it."""

    id: str = Field(pattern=_VECTOR_ID)
    input: str
    # The intermediate form (for the RFC 8785 serialization of args_hash), or null when the
    # algorithm falls back to hashing the raw input. Diagnostic, but part of the vector.
    canonical: str | None = None
    expected: str = Field(min_length=1)
    note: str | None = None


class VectorFile(_Model):
    attribute: str
    algorithm_version: Annotated[int, Field(strict=True, ge=1)]
    vectors: tuple[Vector, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [v.id for v in self.vectors]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            raise ValueError(f"{self.attribute}: duplicate vector ids: {', '.join(duplicates)}")
        return self


Registry = Mapping[str, VectorFile]
"""Vector files keyed by the attribute they pin."""


def _parse(name: str, text: str) -> VectorFile:
    parsed = VectorFile.model_validate(json.loads(text))
    if name != parsed.attribute + _SUFFIX:
        raise ValueError(f"{name}: a vector file is named after its attribute ({parsed.attribute})")
    return parsed


def load_registry(directory: Path) -> Registry:
    """The vector files in `directory` (empty when it does not exist)."""
    if not directory.is_dir():
        return {}
    paths = sorted(directory.glob(f"*{_SUFFIX}"))
    files = [_parse(p.name, p.read_text(encoding="utf-8")) for p in paths]
    return {f.attribute: f for f in files}


def load_registry_from_git(ref: str, directory: str, *, cwd: Path) -> Registry:
    """The vector files in `directory` (a repo-relative path) at revision `ref`.

    A revision without the directory has no vectors. An unknown `ref` is an error: a missing
    baseline must not pass as "no vectors".
    """
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not available")
    listed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, "ls-tree", "-r", "--name-only", ref, "--", directory],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"cannot list {directory} at {ref}: {listed.stderr.strip()}")
    files: list[VectorFile] = []
    for path in sorted(p for p in listed.stdout.splitlines() if p.endswith(_SUFFIX)):
        shown = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "show", f"{ref}:{path}"], cwd=cwd, capture_output=True, text=True, check=False
        )
        if shown.returncode != 0:
            raise RuntimeError(f"cannot read {path} at {ref}: {shown.stderr.strip()}")
        files.append(_parse(Path(path).name, shown.stdout))
    return {f.attribute: f for f in files}


def registry_problems(profile: Profile, registry: Registry) -> list[str]:
    """What is wrong with `registry` as a whole against `profile` (empty when consistent)."""
    problems: list[str] = []
    for attribute, file in registry.items():
        spec = profile.attribute(attribute)
        if spec is None:
            problems.append(f"{attribute}: vectors for an attribute that is not in the profile")
            continue
        declared = spec.source.algorithm_version if isinstance(spec.source, AegisSource) else None
        if declared is None:
            problems.append(f"{attribute}: has vectors but declares no algorithm_version")
        elif declared != file.algorithm_version:
            problems.append(
                f"{attribute}: vectors are for algorithm_version {file.algorithm_version}, "
                f"the profile declares {declared}"
            )
    return problems
