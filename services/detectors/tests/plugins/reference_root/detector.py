"""Test-only reference detector (#15): one info finding on the root with the trace's shape,
and one warn finding per non-root span whose status is Error. Not registered in production."""

from collections.abc import Sequence
from dataclasses import dataclass

from traceblame_detectors.api import (
    Component,
    Detector,
    DetectorContext,
    DetectorSpec,
    EvidenceValue,
    FindingDraft,
    Reads,
    Severity,
    SpanKind,
    TraceView,
)


@dataclass(frozen=True)
class Params:
    min_spans: int = 1
    error_score: float = 0.5


class ReferenceRoot(Detector):
    spec = DetectorSpec(
        name="reference_root",
        code_version="0.1.0",
        component=Component.ORCHESTRATION,
        reads=Reads(
            span_kinds=frozenset(SpanKind),
            attrs=frozenset({"aegis.output.parse_ok", "aegis.output.cited_doc_ids"}),
        ),
        params_type=Params,
        evidence_enums={"kinds": frozenset(k.value for k in SpanKind)},
    )

    def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
        if len(trace.spans) < self.params.min_spans:
            return []
        parse_failures = sum(s.attr("aegis.output.parse_ok") is False for s in trace.spans)
        out = [
            FindingDraft(
                span_id=trace.root_span_id,
                severity=Severity.INFO,
                score=0.1,
                evidence={
                    "spans": len(trace.spans),
                    "kinds": tuple(sorted({s.span_kind for s in trace.spans})),
                    "parse_failures": parse_failures,
                },
            )
        ]
        for s in trace.spans:
            if s.span_id != trace.root_span_id and s.status_code == "Error":
                evidence: dict[str, EvidenceValue] = {"duration_ms": s.duration_ms}
                if s.tool_name:
                    evidence["tool"] = s.tool_name
                out.append(
                    FindingDraft(s.span_id, Severity.WARN, self.params.error_score, evidence)
                )
        return out
