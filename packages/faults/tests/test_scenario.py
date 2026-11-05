"""scenarios/*.yaml: one scenario per (kind, app), every draw inside the kind's schema."""

import random
from pathlib import Path

import pytest

from traceblame_faults import kinds, scenario
from traceblame_faults.plan import Query


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - a seeded draw, not crypto


REASONING_FIELDS = ("{query}", "{previous_steps}", "{available_tools}", "{observation}")


def test_the_packaged_scenarios_cover_every_kind_and_app_once() -> None:
    loaded = scenario.load_all()
    want = {(k.name, app) for k in kinds.KINDS.values() for app in k.apps}
    assert set(loaded) == want
    assert len(loaded) == 8


def test_every_draw_is_a_valid_plan() -> None:
    for (kind, _), sc in scenario.load_all().items():
        for seed in range(50):
            params = scenario.draw(sc, _rng(seed))
            kinds.check_params(kind, params)
            assert params["scenario"] == sc.name
            assert params["scenario_version"] == sc.version


def test_typescript_malformed_output_truncates_only() -> None:
    """The AI SDK may strip a markdown fence before parsing; truncation never parses."""
    sc = scenario.load_all()[("malformed_output", "runbook-agent")]
    assert {scenario.draw(sc, _rng(s))["mode"] for s in range(50)} == {"truncate"}


def test_prompt_variants_name_their_source_and_keep_the_app_templates_usable() -> None:
    py = scenario.load_all()[("prompt_variant", "agentic-rag")]
    ts = scenario.load_all()[("prompt_variant", "runbook-agent")]
    assert len(py.variants) == 2
    assert len(ts.variants) == 2
    for variant in [*py.variants.values(), *ts.variants.values()]:
        assert variant.source
        assert variant.rationale
        assert variant.templates
    for variant in py.variants.values():
        assert set(variant.templates) <= {"system", "reasoning"}
        if "reasoning" in variant.templates:
            assert all(f in variant.templates["reasoning"] for f in REASONING_FIELDS)
    for variant in ts.variants.values():
        assert set(variant.templates) == {"system"}
    drawn = {scenario.draw(py, _rng(s))["variant"] for s in range(50)}
    assert drawn == set(py.variants)


def test_eligibility_by_stratum_and_golden_status() -> None:
    sc = scenario.load_all()[("golden_context_removed", "agentic-rag")]
    ok = Query(
        ref="q", app="agentic-rag", source="cuad", stratum="answerable", golden_status="mapped"
    )
    assert scenario.eligible(sc, ok)
    assert not scenario.eligible(sc, ok.with_(golden_status="unmapped"))
    assert not scenario.eligible(sc, ok.with_(stratum="unanswerable", golden_status="none"))
    assert not scenario.eligible(sc, ok.with_(app="runbook-agent"))
    anything = scenario.load_all()[("tool_failure", "agentic-rag")]
    assert scenario.eligible(anything, ok.with_(stratum="unanswerable", golden_status="none"))


def _write(tmp: Path, text: str) -> Path:
    path = tmp / "s.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "scenario: s\nversion: 1\nkind: nope\napp: agentic-rag\nparams: {}\n",
            "unknown fault kind",
        ),
        (
            "scenario: s\nversion: 1\nkind: tool_failure\napp: runbook-agent\nparams: {}\n",
            "not for runbook-agent",
        ),
        (
            "scenario: s\nversion: 1\nkind: tool_failure\napp: agentic-rag\n"
            "params: {mode: {value: backend_error}, tool: {value: retriever},"
            " call_index: {int: [0, 3]}}\n",
            "call_index",
        ),
        (
            "scenario: s\nversion: 1\nkind: tool_failure\napp: agentic-rag\n"
            "params: {mode: {value: backend_error}, tool: {value: retriever},"
            " call_index: {value: 1}}\nextra: 1\n",
            "extra",
        ),
        (
            "scenario: s\nversion: 1\nkind: tool_failure\napp: agentic-rag\n"
            "params: {mode: {value: backend_error}, tool: {value: retriever}}\n",
            "call_index",
        ),
        (
            "scenario: s\nversion: 1\nkind: tool_failure\napp: agentic-rag\n"
            "params: {mode: {sometimes: x}, tool: {value: retriever}, call_index: {value: 1}}\n",
            "generator",
        ),
    ],
)
def test_bad_scenarios_are_refused(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(scenario.ScenarioError, match=match):
        scenario.load(_write(tmp_path, body))
