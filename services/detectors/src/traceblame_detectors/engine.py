"""The engine: runs the enabled detectors on one trace view (#15). Pure; the online handler and
the offline runner both call `run_trace`, so the same trace gives the same findings in both.

Per detector it builds the detector's projection, calls `detect`, validates the drafts and
turns them into canonical findings. A detector that raises, or returns something invalid, fails
its own run only (`status=error`, `error_kind` = a fixed reason or the exception class name,
never its message).

FRAMEWORK_VERSION covers this module, view.py, canonical.py, policy.py and context.py: what
decides a finding's bytes (and which evidence is accepted) besides the detector.
framework.lock.json pins their sources per version.
"""

import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from traceblame_detectors.canonical import canonical_json, digest, sha256_hex
from traceblame_detectors.config import EnabledDetector
from traceblame_detectors.context import BaselineProvider, DetectorContext, GoldenProvider
from traceblame_detectors.model import EvidenceValue, FindingDraft, Severity
from traceblame_detectors.view import TraceView

FRAMEWORK_VERSION = "1.0.0"

# Bounds of one run's output: they bound the finding.v1 event (PR-B) and the details column.
MAX_FINDINGS_PER_RUN = 64
MAX_EVIDENCE_KEYS = 32
MAX_EVIDENCE_LIST = 256
SCORE_DECIMALS = 4

# finding_id = uuid5(FINDING_NAMESPACE,
#                    "trace_id/detector/detector_version/span_id/revision/input_digest").
FINDING_NAMESPACE = uuid.UUID("8f0b6d52-3c0e-4f43-9d8a-6a1f5f0e2b7c")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RunStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class RejectedError(ValueError):
    """A detector's output broke the contract; the message is a fixed reason."""


@dataclass(frozen=True)
class Finding:
    trace_id: str
    revision_ms: int
    input_digest: str
    # The identity of the run that produced the finding (run_digest()): what readers join on.
    run_digest: str
    span_id: str
    detector: str
    detector_version: str
    component: str
    severity: str
    score: float
    # Sorted (key, value) pairs; lists as tuples.
    evidence: tuple[tuple[str, EvidenceValue], ...]

    @property
    def finding_id(self) -> str:
        return finding_id(
            self.trace_id,
            self.detector,
            self.detector_version,
            self.span_id,
            self.revision_ms,
            self.run_digest,
        )

    def evidence_json(self) -> str:
        return canonical_json(_evidence_obj(self.evidence)).decode("ascii")

    def canonical(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "trace_id": self.trace_id,
            "revision_ms": self.revision_ms,
            "input_digest": self.input_digest,
            "run_digest": self.run_digest,
            "span_id": self.span_id,
            "detector": self.detector,
            "detector_version": self.detector_version,
            "component": self.component,
            "severity": self.severity,
            "score": self.score,
            "evidence": _evidence_obj(self.evidence),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.canonical())


@dataclass(frozen=True)
class DetectorRun:
    trace_id: str
    revision_ms: int
    assembler_version: str
    detector: str
    detector_version: str
    code_version: str
    component: str
    status: RunStatus
    error_kind: str
    input_digest: str
    context_digest: str
    framework_version: str
    run_digest: str
    findings: tuple[Finding, ...]

    @property
    def findings_digest(self) -> str:
        return sha256_hex(canonical_json([f.canonical() for f in self.findings]))

    def canonical(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "revision_ms": self.revision_ms,
            "assembler_version": self.assembler_version,
            "detector": self.detector,
            "detector_version": self.detector_version,
            "code_version": self.code_version,
            "component": self.component,
            "status": self.status.value,
            "error_kind": self.error_kind,
            "input_digest": self.input_digest,
            "context_digest": self.context_digest,
            "framework_version": self.framework_version,
            "run_digest": self.run_digest,
            "findings_digest": self.findings_digest,
            "findings": [f.canonical() for f in self.findings],
        }


@dataclass(frozen=True)
class Providers:
    """Context providers the caller built. The online path never sets `golden`."""

    baseline: BaselineProvider | None = None
    golden: GoldenProvider | None = None


def finding_id(
    trace_id: str,
    detector: str,
    detector_version: str,
    span_id: str,
    revision_ms: int,
    run_digest: str,
) -> str:
    name = f"{trace_id}/{detector}/{detector_version}/{span_id}/{revision_ms}/{run_digest}"
    return str(uuid.uuid5(FINDING_NAMESPACE, name))


def run_digest(
    input_digest: str, context_digest: str, detector_version: str, framework_version: str
) -> str:
    """The identity of a run's complete inputs: its projection of the trace, the context it got,
    its version and the framework's. Two runs with the same run_digest produce the same findings;
    findings and their detector_runs marker are joined on it."""
    return digest(
        {
            "input_digest": input_digest,
            "context_digest": context_digest,
            "detector_version": detector_version,
            "framework_version": framework_version,
        }
    )


def run_trace(
    view: TraceView, detectors: Sequence[EnabledDetector], providers: Providers
) -> tuple[DetectorRun, ...]:
    return tuple(_run_one(view, d, providers) for d in detectors)


def _run_one(view: TraceView, enabled: EnabledDetector, providers: Providers) -> DetectorRun:
    spec = enabled.spec
    projection = view.project(spec.reads)
    input_digest = digest(projection.canonical())
    ctx = DetectorContext(
        trace_id=view.trace_id,
        trace_start_us=view.start_us,
        needs=spec.needs,
        baseline=providers.baseline,
        golden=providers.golden if spec.eval_only else None,
    )
    status, error_kind = RunStatus.OK, ""
    findings: tuple[Finding, ...] = ()
    try:
        drafts = enabled.instance().detect(projection, ctx)
        context_ids = ctx.returned_ids(golden=spec.eval_only)
        identity = _identity(input_digest, digest(ctx.used()), enabled.detector_version)
        findings = _validate(projection, enabled, drafts, identity, context_ids)
    except RejectedError as e:
        status, error_kind = RunStatus.ERROR, str(e)
    except Exception as e:  # a detector bug fails its own run only
        status, error_kind = RunStatus.ERROR, type(e).__name__
    context_digest = digest(ctx.used())
    return DetectorRun(
        trace_id=view.trace_id,
        revision_ms=view.revision_ms,
        assembler_version=view.assembler_version,
        detector=spec.name,
        detector_version=enabled.detector_version,
        code_version=spec.code_version,
        component=spec.component.value,
        status=status,
        error_kind=error_kind,
        input_digest=input_digest,
        context_digest=context_digest,
        framework_version=FRAMEWORK_VERSION,
        run_digest=run_digest(
            input_digest, context_digest, enabled.detector_version, FRAMEWORK_VERSION
        ),
        findings=findings,
    )


def _identity(input_digest: str, context_digest: str, detector_version: str) -> tuple[str, str]:
    """(input_digest, run_digest) that the run's findings carry."""
    return input_digest, run_digest(
        input_digest, context_digest, detector_version, FRAMEWORK_VERSION
    )


def _validate(
    projection: TraceView,
    enabled: EnabledDetector,
    drafts: Sequence[FindingDraft],
    identity: tuple[str, str],
    context_ids: frozenset[str] = frozenset(),
) -> tuple[Finding, ...]:
    if len(drafts) > MAX_FINDINGS_PER_RUN:
        raise RejectedError("too_many_findings")
    span_ids = {s.span_id for s in projection.spans} | {projection.root_span_id}
    allowed = _evidence_strings(projection, enabled.spec.evidence_enums, context_ids)
    out: list[Finding] = []
    for d in drafts:
        if not isinstance(d, FindingDraft):  # pyright: ignore[reportUnnecessaryIsInstance]  # plugins are not type-checked at run time
            raise RejectedError("not_a_finding")
        if d.span_id not in span_ids:
            raise RejectedError("unknown_span")
        if not isinstance(d.severity, Severity):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RejectedError("bad_severity")
        score: object = d.score  # plugins are not type-checked at run time
        if isinstance(score, bool) or not isinstance(score, int | float):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RejectedError("bad_score")
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise RejectedError("bad_score")
        out.append(
            Finding(
                trace_id=projection.trace_id,
                revision_ms=projection.revision_ms,
                input_digest=identity[0],
                run_digest=identity[1],
                span_id=d.span_id,
                detector=enabled.spec.name,
                detector_version=enabled.detector_version,
                component=enabled.spec.component.value,
                severity=d.severity.value,
                score=round(float(score), SCORE_DECIMALS),
                evidence=_evidence(d.evidence, allowed),
            )
        )
    if len({f.span_id for f in out}) != len(out):
        raise RejectedError("duplicate_span")
    return tuple(sorted(out, key=lambda f: f.span_id))


def _evidence_strings(
    projection: TraceView, enums: Mapping[str, frozenset[str]], context_ids: frozenset[str]
) -> dict[str | None, frozenset[str]]:
    """Strings evidence may carry (key None: under any evidence key): ids from the detector's
    own view, id-typed strings its own context returned (baseline bundle digests; golden doc
    ids for eval_only detectors), and each key's declared enum values."""
    ids: set[str] = {projection.root_span_id, projection.bundle_digest, *context_ids}
    for s in projection.spans:
        ids.update((s.span_id, s.tool_name, s.model, s.args_hash, s.bundle_digest, s.provider))
        ids.update(s.retrieved_doc_ids)
        ids.update(s.finish_reasons)
    ids.discard("")
    allowed: dict[str | None, frozenset[str]] = {None: frozenset(ids)}
    allowed.update(enums)
    return allowed


def _evidence(
    evidence: Mapping[str, EvidenceValue], allowed: Mapping[str | None, frozenset[str]]
) -> tuple[tuple[str, EvidenceValue], ...]:
    if len(evidence) > MAX_EVIDENCE_KEYS:
        raise RejectedError("evidence_rejected")
    out: list[tuple[str, EvidenceValue]] = []
    for key, value in evidence.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise RejectedError("evidence_rejected")
        ok = allowed[None] | allowed.get(key, frozenset())
        if isinstance(value, tuple):
            if len(value) > MAX_EVIDENCE_LIST:
                raise RejectedError("evidence_rejected")
            out.append((key, tuple(_scalar(v, ok) for v in value)))
        else:
            out.append((key, _scalar(value, ok)))
    return tuple(sorted(out))


def _scalar(value: object, ok: frozenset[str]) -> bool | int | float | str:
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RejectedError("evidence_rejected")
        return value
    if isinstance(value, str) and value in ok:
        return value
    raise RejectedError("evidence_rejected")


def _evidence_obj(evidence: tuple[tuple[str, EvidenceValue], ...]) -> dict[str, Any]:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in evidence}
