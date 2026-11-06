"""The detector plugin contract (#15). A plugin imports only from this module.

A detector is a class with a `spec` and a `detect` method. The engine constructs it with its
params (a frozen dataclass of behaviour params: thresholds, windows, switches) and calls
`detect` with its projection of the trace and a context that answers only its declared needs.
It returns findings; the engine validates, versions and canonicalizes them.

Rules a detector follows (the engine enforces what it can, tests the rest):
- pure: the same view, params and context give the same findings;
- one finding per span at most; a trace-level finding names the root span;
- evidence holds numbers, booleans, and strings that are ids taken from the view it got
  (span ids, doc ids, tool names, models, finish reasons, args hashes, bundle digests,
  providers) or values of its declared `evidence_enums`; never content;
- no I/O: no storage, files, network or environment.
"""

import abc
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from traceblame_detectors.context import DetectorContext, UndeclaredContextError
from traceblame_detectors.model import (
    Component,
    ContextNeed,
    EvidenceValue,
    FindingDraft,
    Reads,
    Scalar,
    ServiceBaseline,
    Severity,
    SpanKind,
)
from traceblame_detectors.view import AttrValue, SpanView, TraceView, UndeclaredReadError

__all__ = [
    "AttrValue",
    "Component",
    "ContextNeed",
    "Detector",
    "DetectorContext",
    "DetectorSpec",
    "EvidenceValue",
    "FindingDraft",
    "Reads",
    "Scalar",
    "ServiceBaseline",
    "Severity",
    "SpanKind",
    "SpanView",
    "TraceView",
    "UndeclaredContextError",
    "UndeclaredReadError",
]

NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class DetectorSpec:
    name: str
    code_version: str
    component: Component
    reads: Reads
    # A frozen dataclass of behaviour params with defaults for every field.
    params_type: type[Any]
    needs: frozenset[ContextNeed] = frozenset()
    eval_only: bool = False
    # Fixed string values evidence may carry besides ids from the view, per evidence key.
    evidence_enums: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])


class Detector(abc.ABC):
    spec: ClassVar[DetectorSpec]

    def __init__(self, params: Any) -> None:
        self.params = params

    @abc.abstractmethod
    def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]: ...
