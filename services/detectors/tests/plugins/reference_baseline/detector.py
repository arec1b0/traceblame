"""Test-only reference detector with context: the trace's bundle digest against its service's
baseline as of the trace's start."""

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
    score: float = 0.7


class ReferenceBaseline(Detector):
    spec = DetectorSpec(
        name="reference_baseline",
        code_version="0.1.0",
        component=Component.PROMPT,
        reads=Reads(span_kinds=frozenset({SpanKind.AGENT})),
        params_type=Params,
        needs=frozenset({ContextNeed.SERVICE_BASELINE}),
    )

    def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
        baseline = ctx.service_baseline(trace.service, trace.start_us)
        if baseline is None or baseline.bundle_digest == trace.bundle_digest:
            return []
        return [
            FindingDraft(
                trace.root_span_id,
                Severity.WARN,
                self.params.score,
                {"digest": trace.bundle_digest, "baseline_traces": baseline.trace_count},
            )
        ]
