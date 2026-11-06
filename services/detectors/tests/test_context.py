"""Context providers: as-of baselines, and only declared needs answered."""

import json
from pathlib import Path

import pytest

from traceblame_detectors.context import (
    DetectorContext,
    StaticBaselineProvider,
    UndeclaredContextError,
)
from traceblame_detectors.model import ContextNeed, ServiceBaseline


def _b(end: int, digest: str) -> ServiceBaseline:
    return ServiceBaseline("svc", digest, end - 100, end, 10, {"x": 1.0})


def test_the_newest_baseline_that_ended_before_the_point_is_used() -> None:
    p = StaticBaselineProvider([_b(300, "c"), _b(100, "a"), _b(200, "b")])
    assert p.service_baseline("svc", 99) is None
    assert p.service_baseline("svc", 250) == _b(200, "b")
    assert p.service_baseline("svc", 300) == _b(300, "c")
    assert p.service_baseline("other", 1000) is None


def test_baselines_load_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "baselines.json"
    path.write_text(json.dumps([_b(100, "a").canonical()]))
    assert StaticBaselineProvider.from_file(path).service_baseline("svc", 100) == _b(100, "a")


def test_only_declared_needs_are_answered_and_every_answer_is_recorded() -> None:
    ctx = DetectorContext(
        trace_id="t",
        trace_start_us=500,
        needs=frozenset({ContextNeed.SERVICE_BASELINE}),
        baseline=StaticBaselineProvider([_b(100, "a")]),
    )
    with pytest.raises(UndeclaredContextError):
        ctx.golden_doc_ids()
    assert ctx.service_baseline("svc", 500) == _b(100, "a")
    assert ctx.service_baseline("nope", 400) is None
    assert ctx.used() == [
        {
            "need": "service_baseline",
            "service": "svc",
            "as_of_us": 500,
            "value": _b(100, "a").canonical(),
        },
        {"need": "service_baseline", "service": "nope", "as_of_us": 400, "value": None},
    ]


def test_golden_without_a_provider_is_none() -> None:
    ctx = DetectorContext(
        trace_id="t", trace_start_us=0, needs=frozenset({ContextNeed.EVAL_GOLDEN})
    )
    assert ctx.golden_doc_ids() is None
    assert ctx.used() == [{"need": "eval_golden", "value": None}]
