"""The engine: byte-identical findings, provenance of evidence, failures stay in their run."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest

from traceblame_detectors.api import (
    Detector,
    DetectorContext,
    FindingDraft,
    Reads,
    Severity,
    SpanKind,
    TraceView,
)
from traceblame_detectors.config import DetectorConfig, load_config
from traceblame_detectors.context import StaticBaselineProvider
from traceblame_detectors.engine import (
    FRAMEWORK_VERSION,
    MAX_FINDINGS_PER_RUN,
    DetectorRun,
    Providers,
    RunStatus,
    finding_id,
    run_trace,
)
from traceblame_detectors.model import ServiceBaseline
from traceblame_detectors.policy import DECLARABLE
from traceblame_detectors.testing import ROOT, TRACE_ID, span_rows, summary_row
from traceblame_detectors.view import build_view

Make = Callable[..., DetectorConfig]


def _view(rows: Sequence[dict[str, Any]] | None = None, **summary: Any) -> TraceView:
    return build_view(summary_row(**summary), span_rows() if rows is None else rows, DECLARABLE)


def _run(
    config: DetectorConfig, view: TraceView, providers: Providers | None = None
) -> DetectorRun:
    (run,) = run_trace(view, config.detectors, providers or Providers())
    return run


def _canonical(runs: Sequence[DetectorRun]) -> list[bytes]:
    return [f.canonical_bytes() for r in runs for f in r.findings]


def test_the_reference_detector_finds_the_root_and_the_failed_tool(make_config: Make) -> None:
    run = _run(make_config(reference_root={}), _view())
    assert run.status is RunStatus.OK
    assert run.error_kind == ""
    assert [(f.span_id, f.severity, f.score) for f in run.findings] == [
        (ROOT, "info", 0.1),
        ("2222222222222222", "warn", 0.5),
    ]
    root = run.findings[0]
    assert root.evidence_json() == (
        '{"kinds":["agent","llm","retrieval","tool"],"parse_failures":1,"spans":4}'
    )
    assert run.framework_version == FRAMEWORK_VERSION
    assert run.findings[1].evidence_json() == '{"duration_ms":3.5,"tool":"search_contract"}'


def test_the_same_trace_gives_byte_identical_findings(make_config: Make) -> None:
    config = make_config(reference_root={})
    rows = span_rows()
    first = run_trace(_view(rows), config.detectors, Providers())
    shuffled = rows[2:] + rows[:2]  # another order, fixed
    again = run_trace(_view(shuffled), config.detectors, Providers())
    assert first == again
    assert _canonical(first) == _canonical(again)
    assert _canonical(first)
    assert [r.findings_digest for r in first] == [r.findings_digest for r in again]
    assert [r.input_digest for r in first] == [r.input_digest for r in again]


def test_a_new_revision_gives_new_finding_ids(make_config: Make) -> None:
    config = make_config(reference_root={})
    a = _run(config, _view())
    b = _run(config, _view(revision_ms=1_793_700_000_999))
    assert a.input_digest != b.input_digest
    assert {f.finding_id for f in a.findings}.isdisjoint(f.finding_id for f in b.findings)
    f = a.findings[0]
    assert f.finding_id == finding_id(
        TRACE_ID, f.detector, f.detector_version, f.span_id, f.revision_ms, f.input_digest
    )


def test_input_digest_covers_only_what_the_detector_reads(make_config: Make) -> None:
    config = make_config(reference_baseline={})  # reads agent spans only
    rows = span_rows()
    other = [dict(r) for r in rows]
    other[3]["output_tokens"] = 999  # an llm span: not in its projection
    assert _run(config, _view(rows)).input_digest == _run(config, _view(other)).input_digest
    other[0]["duration_ms"] = 1.0  # the agent span
    assert _run(config, _view(rows)).input_digest != _run(config, _view(other)).input_digest


class _Bad(Detector):
    drafts: Sequence[Any] = ()
    raises: Exception | None = None

    def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
        if self.raises is not None:
            raise self.raises
        return self.drafts


def _bad(registry: dict[str, type[Detector]], **attrs: Any) -> DetectorConfig:
    base = registry["reference_root"]
    cls = type("Bad", (_Bad,), {"spec": replace(base.spec, name="bad"), **attrs})
    return load_config(
        {"detectors": {"bad": {}, "reference_root": {}}}, {"bad": cls, "reference_root": base}
    )


@pytest.mark.parametrize(
    ("drafts", "error_kind"),
    [
        ([FindingDraft(ROOT, Severity.INFO, 0.5, {"who": "ops@example.com"})], "evidence_rejected"),
        (
            [FindingDraft(ROOT, Severity.INFO, 0.5, {"kinds": "agent", "tool": "agent"})],
            "evidence_rejected",
        ),
        ([FindingDraft(ROOT, Severity.INFO, 0.5, {"x": float("nan")})], "evidence_rejected"),
        ([FindingDraft(ROOT, Severity.INFO, 0.5, {"Bad Key": 1})], "evidence_rejected"),
        (
            [FindingDraft(ROOT, Severity.INFO, 0.5, {f"k{i}": i for i in range(33)})],
            "evidence_rejected",
        ),
        ([FindingDraft(ROOT, Severity.INFO, 0.5, {"xs": tuple(range(257))})], "evidence_rejected"),
        ([FindingDraft(ROOT, Severity.INFO, 0.5, {"x": None})], "evidence_rejected"),  # pyright: ignore[reportArgumentType]
        ([FindingDraft("ffffffffffffffff", Severity.INFO, 0.5)], "unknown_span"),
        ([FindingDraft(ROOT, Severity.INFO, 1.5)], "bad_score"),
        ([FindingDraft(ROOT, Severity.INFO, float("nan"))], "bad_score"),
        ([FindingDraft(ROOT, Severity.INFO, True)], "bad_score"),
        ([FindingDraft(ROOT, "warn", 0.5)], "bad_severity"),  # pyright: ignore[reportArgumentType]
        ([FindingDraft(ROOT, Severity.INFO, 0.5)] * 2, "duplicate_span"),
        (
            [FindingDraft(ROOT, Severity.INFO, 0.5)] * (MAX_FINDINGS_PER_RUN + 1),
            "too_many_findings",
        ),
        ([{"span_id": ROOT}], "not_a_finding"),
    ],
)
def test_output_that_breaks_the_contract_fails_its_run_only(
    registry: dict[str, type[Detector]], drafts: list[Any], error_kind: str
) -> None:
    bad, good = run_trace(_view(), _bad(registry, drafts=drafts).detectors, Providers())
    assert (bad.detector, bad.status, bad.error_kind, bad.findings) == (
        "bad",
        RunStatus.ERROR,
        error_kind,
        (),
    )
    assert good.status is RunStatus.OK
    assert good.findings


def test_ids_from_the_view_and_declared_enums_are_allowed_evidence(
    registry: dict[str, type[Detector]],
) -> None:
    evidence = {
        "docs": ("cuad-0123456789ab-3",),
        "model": "qwen2.5:7b",
        "provider": "openai",
        "finish": "stop",
        "args": "0123456789abcdef",
        "digest": "sha256:" + "a" * 64,
        "span": "3333333333333333",
        "kinds": ("llm", "tool"),
        "ok": False,
        "n": 3,
    }
    bad, _ = run_trace(
        _view(),
        _bad(registry, drafts=[FindingDraft(ROOT, Severity.INFO, 0.25, evidence)]).detectors,
        Providers(),
    )
    assert bad.status is RunStatus.OK, bad.error_kind


def test_an_id_outside_the_detectors_projection_is_rejected(
    registry: dict[str, type[Detector]],
) -> None:
    base = registry["reference_root"]
    spec = replace(base.spec, name="bad", reads=Reads(frozenset({SpanKind.AGENT})))
    cls = type(
        "Bad",
        (_Bad,),
        {
            "spec": spec,
            "drafts": [FindingDraft(ROOT, Severity.INFO, 0.5, {"tool": "search_contract"})],
        },
    )
    config = load_config({"detectors": {"bad": {}}}, {"bad": cls})
    assert _run(config, _view()).error_kind == "evidence_rejected"


@pytest.mark.parametrize(
    ("exc", "kind"),
    [(RuntimeError("secret prompt text"), "RuntimeError"), (KeyError("x"), "KeyError")],
)
def test_an_exception_is_recorded_by_class_name_only(
    registry: dict[str, type[Detector]], exc: Exception, kind: str
) -> None:
    bad, good = run_trace(_view(), _bad(registry, raises=exc).detectors, Providers())
    assert (bad.status, bad.error_kind) == (RunStatus.ERROR, kind)
    assert "secret" not in repr(bad.canonical())
    assert good.status is RunStatus.OK


def test_an_undeclared_read_fails_the_run(registry: dict[str, type[Detector]]) -> None:
    class Reader(_Bad):
        def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
            for s in trace.spans:
                s.attr("gen_ai.response.model")
            return []

    base = registry["reference_root"]
    cls = type("R", (Reader,), {"spec": replace(base.spec, name="reader")})
    config = load_config({"detectors": {"reader": {}}}, {"reader": cls})
    assert _run(config, _view()).error_kind == "UndeclaredReadError"


def test_score_is_quantized(registry: dict[str, type[Detector]]) -> None:
    drafts = [FindingDraft(ROOT, Severity.ERROR, 0.123456789)]
    bad, _ = run_trace(_view(), _bad(registry, drafts=drafts).detectors, Providers())
    assert bad.findings[0].score == 0.1235


BASELINE = ServiceBaseline(
    service="agentic-rag",
    bundle_digest="sha256:" + "b" * 64,
    window_start_us=1_793_000_000_000_000,
    window_end_us=1_793_600_000_000_000,
    trace_count=120,
    outcomes={"tool_failure_rate": 0.02},
)


def test_context_answers_are_part_of_the_run(make_config: Make) -> None:
    config = make_config(reference_baseline={})
    without = _run(config, _view())
    assert without.status is RunStatus.OK
    assert without.findings == ()
    with_baseline = _run(config, _view(), Providers(baseline=StaticBaselineProvider([BASELINE])))
    assert [f.evidence_json() for f in with_baseline.findings] == [
        '{"baseline_traces":120,"digest":"sha256:' + "a" * 64 + '"}'
    ]
    assert with_baseline.context_digest != without.context_digest
    assert with_baseline.input_digest == without.input_digest


def test_golden_ids_reach_only_an_eval_only_detector(make_config: Make) -> None:
    class Golden:
        def golden_doc_ids(self, trace_id: str) -> tuple[str, ...] | None:
            return ("cuad-ffffffffffff-1",)

    providers = Providers(golden=Golden())
    config = make_config("eval", reference_golden={}, reference_root={})
    golden, root = run_trace(_view(), config.detectors, providers)
    assert [f.evidence_json() for f in golden.findings] == ['{"golden_in_topk":false}']
    assert root.status is RunStatus.OK

    # Without eval_only, the engine gives the detector no golden provider at all.
    spec = replace(config.detectors[0].spec, eval_only=False)
    leaked = replace(config.detectors[0], cls=type("G", (config.detectors[0].cls,), {"spec": spec}))
    (run,) = run_trace(_view(), [leaked], providers)
    assert run.findings == ()
    assert run.status is RunStatus.OK


def test_asking_for_undeclared_context_fails_the_run(registry: dict[str, type[Detector]]) -> None:
    class Asker(_Bad):
        def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
            ctx.service_baseline(trace.service, trace.start_us)
            return []

    base = registry["reference_root"]
    cls = type("A", (Asker,), {"spec": replace(base.spec, name="asker")})
    config = load_config({"detectors": {"asker": {}}}, {"asker": cls})
    assert _run(config, _view()).error_kind == "UndeclaredContextError"


def test_a_baseline_from_after_the_trace_start_is_refused(
    registry: dict[str, type[Detector]],
) -> None:
    class Ahead(_Bad):
        def detect(self, trace: TraceView, ctx: DetectorContext) -> Sequence[FindingDraft]:
            ctx.service_baseline(trace.service, trace.start_us + 1)
            return []

    base = registry["reference_baseline"]
    cls = type("A", (Ahead,), {"spec": replace(base.spec, name="ahead")})
    config = load_config({"detectors": {"ahead": {}}}, {"ahead": cls})
    assert _run(config, _view()).error_kind == "ValueError"


def test_a_run_without_findings_has_the_empty_digest(make_config: Make) -> None:
    run = _run(make_config(reference_root={"params": {"min_spans": 99}}), _view())
    assert run.findings == ()
    assert run.findings_digest == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
