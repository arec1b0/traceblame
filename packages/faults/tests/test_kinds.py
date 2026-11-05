"""The six fault kinds and their closed parameter schemas: no free text can reach
injections.params."""

from typing import Any

import pytest

from traceblame_faults import kinds

DIGEST = "sha256:" + "a" * 64


def test_six_kinds_over_five_components_with_orchestration_twice() -> None:
    assert len(kinds.KINDS) == 6
    by_component: dict[str, list[str]] = {}
    for kind in kinds.KINDS.values():
        by_component.setdefault(kind.component, []).append(kind.name)
    assert set(by_component) == set(kinds.COMPONENTS)
    assert sorted(by_component["orchestration"]) == [
        "output_budget_exhausted",
        "step_memory_loss",
    ]
    assert all(len(v) == 1 for k, v in by_component.items() if k != "orchestration")


def test_typescript_carries_only_model_and_prompt() -> None:
    ts = {k.name for k in kinds.KINDS.values() if "runbook-agent" in k.apps}
    assert ts == {"malformed_output", "prompt_variant"}
    assert all("agentic-rag" in k.apps for k in kinds.KINDS.values())


def test_every_kind_names_its_marker_span_per_app() -> None:
    for kind in kinds.KINDS.values():
        assert set(kind.marker) == set(kind.apps)
        assert set(kind.marker.values()) <= {"retriever", "tool", "llm", "agent"}


def _common(**extra: Any) -> dict[str, Any]:
    return {"scenario": "s-1", "scenario_version": 1, "faults_version": "0.1.0"} | extra


def test_planned_params_pass() -> None:
    kinds.check_params(
        "malformed_output",
        _common(mode="truncate", cut_fraction=0.4, call_index=1, persist="once"),
    )


def test_applied_params_may_carry_the_applied_only_fields() -> None:
    kinds.check_params(
        "golden_context_removed",
        _common(mode="remove", top_k=5, removed_n=1, retrievals=2, baseline_digest=DIGEST),
        applied=True,
    )


def test_applied_only_fields_are_refused_in_a_plan() -> None:
    with pytest.raises(kinds.ParamsError, match="removed_n"):
        kinds.check_params("golden_context_removed", _common(mode="remove", top_k=5, removed_n=1))


@pytest.mark.parametrize(
    ("kind", "params", "match"),
    [
        ("tool_failure", {"mode": "backend_error", "tool": "retriever"}, "call_index"),
        (
            "tool_failure",
            {"mode": "backend_error", "tool": "retriever", "call_index": 1, "note": "x"},
            "note",
        ),
        ("tool_failure", {"mode": "made up", "tool": "retriever", "call_index": 1}, "mode"),
        (
            "output_budget_exhausted",
            {"max_tokens": 0, "call_index": 1, "persist": "once"},
            "max_tokens",
        ),
        (
            "output_budget_exhausted",
            {"max_tokens": True, "call_index": 1, "persist": "once"},
            "max_tokens",
        ),
        (
            "malformed_output",
            {"mode": "fence", "cut_fraction": 2.0, "call_index": 1, "persist": "once"},
            "cut_fraction",
        ),
        ("prompt_variant", {"variant": "the whole prompt text goes here"}, "variant"),
        ("step_memory_loss", {"from_iteration": 1}, "from_iteration"),
        ("nope", {}, "unknown fault kind"),
    ],
)
def test_bad_params_are_refused(kind: str, params: dict[str, Any], match: str) -> None:
    with pytest.raises(kinds.ParamsError, match=match):
        kinds.check_params(kind, _common(**params))


def test_a_digest_slot_takes_only_a_sha256_digest() -> None:
    good = _common(variant="direct-answer", bundle_digest=DIGEST, baseline_digest=DIGEST)
    kinds.check_params("prompt_variant", good, applied=True)
    with pytest.raises(kinds.ParamsError, match="bundle_digest"):
        kinds.check_params(
            "prompt_variant", good | {"bundle_digest": "You are a helpful"}, applied=True
        )


def test_an_id_list_takes_only_ids() -> None:
    with pytest.raises(kinds.ParamsError, match="distractor_doc"):
        kinds.check_params(
            "golden_context_removed",
            _common(mode="distractor", top_k=5, distractor_doc="the lease shall renew"),
            applied=True,
        )


def test_params_json_is_canonical() -> None:
    a = kinds.params_json({"b": 1, "a": "x"})
    assert a == '{"a":"x","b":1}'
