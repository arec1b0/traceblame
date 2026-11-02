"""Golden span fixtures: real OTLP/JSON exports plus a provenance record each."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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


class AppInfo(_Model):
    """The application a capture ran (app captures only)."""

    repository: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class ShimInfo(_Model):
    package: str
    version: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class DatasetInfo(_Model):
    """Where an app capture's queries come from, and how they were sampled."""

    name: str
    source: str
    archive_sha256: str = Field(pattern=_SHA256)
    license: str
    seed: int
    n: int
    ratio: float
    stratum: Literal["all", "answerable", "unanswerable"]
    qids: tuple[str, ...]


class EmbeddingModelInfo(_Model):
    name: str
    revision: str


class RunInfo(_Model):
    script: str
    script_sha256: str = Field(pattern=_SHA256)
    outcomes: dict[str, int]
    report: str


class ImageRunInfo(_Model):
    """A run of an application image in the kind cluster (the runbook agent, #13)."""

    report: str
    eval_run_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    tenant: str
    image: str
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RuleFile(_Model):
    path: str
    sha256: str = Field(pattern=_SHA256)


class RuleOverride(_Model):
    """One field of an alert rule that differs from its upstream text."""

    rule: str
    upstream: str
    field: str
    original: str | None
    used: str | None


class RulesInfo(_Model):
    """Where the run's alert rules come from, and every field that differs from there."""

    repository: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[RuleFile, ...] = Field(min_length=1)
    license: str
    verbatim: tuple[str, ...] = Field(min_length=1)
    overrides: tuple[RuleOverride, ...]


class StepInfo(_Model):
    action: str
    status: str


class ApprovalInfo(_Model):
    action: str
    decision: Literal["approve", "reject", "timeout"]


class TaskInfo(_Model):
    """One alert the agent handled: the fault's seeded parameters, and what the agent did."""

    task: str
    scenario: str
    order: int = Field(ge=0)
    params: dict[str, Any]
    alert: str
    expected_runbook: str
    runbook: str
    status: str
    steps: tuple[StepInfo, ...]
    approval_policy: Literal["approve", "reject", "timeout"] | None
    approvals: tuple[ApprovalInfo, ...]
    alerts_received: int = Field(ge=0)
    # Alertmanager's webhook requests during the task, and the failed ones among them.
    webhooks: int = Field(ge=0)
    webhooks_failed: int = Field(ge=0)
    started_at: datetime
    # The tee session (one fixture) the task was exported in.
    session: str = Field(min_length=1)


class ScenarioRunInfo(_Model):
    """The alert scenario of an application run with real faults (#13)."""

    seed: int
    per_scenario: int = Field(ge=1)
    alert: str
    expected_runbook: str
    rules: RulesInfo
    approver: str = Field(pattern=r"^scripted:-?[0-9]+$")
    approval_timeout_ms: int = Field(ge=1000)
    tasks: tuple[TaskInfo, ...] = Field(min_length=1)


class Provenance(_Model):
    """How a fixture was captured. Written by tools/fixtures/capture.py or, for real
    application runs, tools/fixtures/capture_agentic_rag.py and capture_runbook_agent.py;
    never by hand."""

    sdk: Literal["openinference", "openllmetry", "otel-genai"]
    scenario: str
    captured_at: datetime
    capture_script: str
    capture_script_sha256: str = Field(pattern=_SHA256)
    # SDK captures share tools/fixtures/_capture_common.py; app captures have no such module.
    capture_common_sha256: str | None = Field(default=None, pattern=_SHA256)
    # The capture's runtime: python and uv, or (the TypeScript shim) node and pnpm.
    python: str | None = None
    uv: str | None = None
    node: str | None = None
    pnpm: str | None = None
    packages: dict[str, str]
    env: dict[str, str]
    # True only for *_content scenarios: content capture was on, so the export carries
    # prompt/completion text on purpose (the normalizer's redaction tests need it).
    content_capture: bool = False
    # App captures: the capture checked that no query or document text was exported.
    content_check: Literal["passed"] | None = None
    model: ModelInfo
    endpoint: str
    collector: str
    fixture_sha256: str = Field(pattern=_SHA256)
    app: AppInfo | None = None
    shim: ShimInfo | None = None
    dataset: DatasetInfo | None = None
    embedding_model: EmbeddingModelInfo | None = None
    run: RunInfo | ImageRunInfo | None = None
    scenario_run: ScenarioRunInfo | None = None
    # Image pins of the monitoring that fired the run's alerts.
    monitoring: dict[str, str] | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _one_runtime(self) -> "Provenance":
        python = (self.python is not None, self.uv is not None)
        node = (self.node is not None, self.pnpm is not None)
        if {python, node} != {(True, True), (False, False)}:
            raise ValueError("set python and uv, or node and pnpm")
        return self


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
