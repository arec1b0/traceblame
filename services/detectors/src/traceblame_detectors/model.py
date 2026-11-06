"""Plain types shared by the detector contract, the trace view and the context."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Component(StrEnum):
    """What a finding blames (design §1.1); `unknown` is the attributor's verdict, not a
    detector's."""

    RETRIEVAL = "retrieval"
    PROMPT = "prompt"
    TOOL = "tool"
    MODEL = "model"
    ORCHESTRATION = "orchestration"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class SpanKind(StrEnum):
    """aegis.span.kind of profile v1."""

    LLM = "llm"
    EMBEDDING = "embedding"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    AGENT = "agent"
    CHAIN = "chain"


class ContextNeed(StrEnum):
    """Read-only context beyond the trace that a detector may declare."""

    SERVICE_BASELINE = "service_baseline"
    # Eval-only: the golden chunk ids of the trace's source query (eval_traces.golden_doc_ids).
    EVAL_GOLDEN = "eval_golden"


type Scalar = bool | int | float | str
type EvidenceValue = Scalar | tuple[Scalar, ...]


@dataclass(frozen=True)
class Reads:
    """What a detector reads of a trace: the span kinds it sees and the span attributes of the
    attrs map it may look up (columns of the view are always there)."""

    span_kinds: frozenset[SpanKind]
    attrs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FindingDraft:
    """What a detector returns for one span. A trace-level finding names the root span."""

    span_id: str
    severity: Severity
    score: float
    evidence: Mapping[str, EvidenceValue] = field(default_factory=dict[str, EvidenceValue])


@dataclass(frozen=True)
class ServiceBaseline:
    """A service's baseline as of a point in time: the bundle digest it ran under and its
    outcome statistics over a window that ended before that point.

    Providers compute it from production data (traces and their summaries), never from eval
    labels: injections, eval_traces and the run's recorded baseline digest are ground truth
    (CLAUDE.md rule 9). The outcome names are defined by the detectors that use them (#16).
    """

    service: str
    bundle_digest: str
    window_start_us: int
    window_end_us: int
    trace_count: int
    outcomes: Mapping[str, float] = field(default_factory=dict[str, float])

    def canonical(self) -> dict[str, object]:
        return {
            "service": self.service,
            "bundle_digest": self.bundle_digest,
            "window_start_us": self.window_start_us,
            "window_end_us": self.window_end_us,
            "trace_count": self.trace_count,
            "outcomes": dict(sorted(self.outcomes.items())),
        }
