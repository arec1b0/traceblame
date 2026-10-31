"""Golden span fixtures: real OTLP/JSON exports plus a provenance record each."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

FIXTURE_SUFFIX = ".otlp.json"
PROVENANCE_SUFFIX = ".provenance.yaml"

_SHA256 = r"^[0-9a-f]{64}$"


class FixtureError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelInfo(_Model):
    name: str
    digest: str
    server: str
    server_version: str


class Provenance(_Model):
    """How a fixture was captured. Written by tools/fixtures/capture.py, never by hand."""

    sdk: Literal["openinference", "openllmetry", "otel-genai"]
    scenario: str
    captured_at: datetime
    capture_script: str
    capture_script_sha256: str = Field(pattern=_SHA256)
    capture_common_sha256: str = Field(pattern=_SHA256)
    python: str
    uv: str
    packages: dict[str, str]
    env: dict[str, str]
    model: ModelInfo
    endpoint: str
    collector: str
    fixture_sha256: str = Field(pattern=_SHA256)
    notes: tuple[str, ...] = ()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Fixture:
    root: Path
    path: Path

    @property
    def id(self) -> str:
        """Path relative to the fixtures root, without the suffix."""
        return self.path.relative_to(self.root).as_posix().removesuffix(FIXTURE_SUFFIX)

    @property
    def provenance_path(self) -> Path:
        return self.path.with_name(self.path.name.removesuffix(FIXTURE_SUFFIX) + PROVENANCE_SUFFIX)

    def provenance(self) -> Provenance:
        try:
            data = yaml.safe_load(self.provenance_path.read_text(encoding="utf-8"))
            return Provenance.model_validate(data)
        except ValidationError as exc:
            raise FixtureError(f"{self.id}: invalid provenance: {exc}") from exc

    def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def hash_matches(self) -> bool:
        return sha256_file(self.path) == self.provenance().fixture_sha256


def discover_fixtures(root: Path) -> list[Fixture]:
    """Every fixture under `root`, sorted; each must have a provenance file and vice versa."""
    fixtures = [Fixture(root, p) for p in sorted(root.rglob(f"*{FIXTURE_SUFFIX}"))]
    for fixture in fixtures:
        if not fixture.provenance_path.is_file():
            raise FixtureError(f"{fixture.id}: missing provenance {fixture.provenance_path.name}")
    for provenance in sorted(root.rglob(f"*{PROVENANCE_SUFFIX}")):
        expected = provenance.name.removesuffix(PROVENANCE_SUFFIX) + FIXTURE_SUFFIX
        if not provenance.with_name(expected).is_file():
            raise FixtureError(f"{provenance}: no fixture {expected}")
    return sorted(fixtures, key=lambda f: f.id)
