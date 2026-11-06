"""Test-only eval_only reference detector: golden_in_topk per retrieval span."""

from collections.abc import Sequence
from dataclasses import dataclass

from traceblame_detectors.api import (
    Component,
    ContextNeed,
    Detector,
    DetectorContext,
    DetectorSpec,
    FindingDraft,
    Reads,
    Severity,
    SpanKind,
    TraceView,
)


@dataclass(frozen=True)
class Params:
    top_k: int = 5


class ReferenceGolden(Detector):
    spec = DetectorSpec(
        name="reference_golden",
        code_version="0.1.0",
        component=Component.RETRIEVAL,
        reads=Reads(span_kinds=frozenset({SpanKind.RETRIEVAL})),
        params_type=Params,
        needs=frozenset({ContextNeed.EVAL_GOLDEN}),
        eval_only=True,
    )

    def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
        golden = set(ctx.golden_doc_ids() or ())
        if not golden:
            return []
        return [
            FindingDraft(
                s.span_id,
                Severity.WARN,
                1.0,
                {"golden_in_topk": False},
            )
            for s in trace.spans
            if not golden & set(s.retrieved_doc_ids[: self.params.top_k])
        ]
