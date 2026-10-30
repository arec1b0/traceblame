import copy
from typing import Any

import pytest

from traceblame_contracts.compat import diff_profiles
from traceblame_contracts.profile import Profile


def _attr(profile: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in profile["attributes"] if a["name"] == name)


def _rules(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    return [b.rule for b in diff_profiles(Profile.model_validate(old), Profile.model_validate(new))]


@pytest.fixture
def old(minimal_profile: dict[str, Any]) -> dict[str, Any]:
    return minimal_profile


@pytest.fixture
def new(minimal_profile: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(minimal_profile)


def test_identical_profiles_have_no_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    assert _rules(old, new) == []


def test_removed_attribute_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["attributes"] = [a for a in new["attributes"] if a["name"] != "gen_ai.usage.input_tokens"]
    assert _rules(old, new) == ["attribute_removed"]


def test_type_change_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.usage.input_tokens")["type"] = "double"
    assert _rules(old, new) == ["type_changed"]


def test_tightened_requirement_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    spec = _attr(new, "gen_ai.usage.input_tokens")
    spec["requirement"] = {"llm": "required"}
    del spec["condition"]
    assert _rules(old, new) == ["requirement_tightened"]


def test_loosened_requirement_is_compatible(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["requirement"] = {"llm": "recommended"}
    assert _rules(old, new) == []


def test_new_optional_attribute_is_compatible(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["attributes"].append(
        {
            "name": "gen_ai.response.model",
            "scope": "span",
            "type": "string",
            "source": {"semconv": "gen_ai.response.model"},
            "requirement": {"llm": "recommended"},
            "brief": "x",
        }
    )
    assert _rules(old, new) == []


def test_new_required_attribute_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["attributes"].append(
        {
            "name": "gen_ai.response.model",
            "scope": "span",
            "type": "string",
            "source": {"semconv": "gen_ai.response.model"},
            "requirement": {"llm": "required"},
            "brief": "x",
        }
    )
    assert _rules(old, new) == ["required_attribute_added"]


def test_removed_enum_value_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    old["span_kinds"]["tool"] = {"description": "Tool call.", "operations": []}
    _attr(old, "aegis.span.kind")["enum"] = ["llm", "chain", "tool"]
    assert _rules(old, new) == ["span_kind_removed", "enum_value_removed"]


def test_added_enum_value_is_compatible(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["span_kinds"]["tool"] = {"description": "Tool call.", "operations": []}
    _attr(new, "aegis.span.kind")["enum"] = ["llm", "chain", "tool"]
    assert _rules(old, new) == []


def test_removed_operation_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    old["span_kinds"]["llm"]["operations"] = ["chat", "text_completion"]
    assert _rules(old, new) == ["operation_removed"]


def test_constraining_operations_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["span_kinds"]["chain"]["operations"] = ["invoke_workflow"]
    assert _rules(old, new) == ["operations_constrained"]


def test_new_pattern_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["pattern"] = "^[a-z]+$"
    assert _rules(old, new) == ["constraint_changed"]


def test_new_forbidden_attribute_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["forbidden"].append({"name": "gen_ai.output.messages", "reason": "content"})
    assert _rules(old, new) == ["forbidden_added"]


def test_semconv_pin_change_breaks(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["semconv"]["version"] = "1.44.0"
    assert _rules(old, new) == ["semconv_pin_changed"]


def test_different_major_is_not_compared(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["major"] = 2
    _attr(new, "aegis.profile.version")["const"] = "aegis-telemetry/2"
    new["attributes"] = [a for a in new["attributes"] if a["name"] != "gen_ai.usage.input_tokens"]
    assert _rules(old, new) == []


def test_break_names_the_subject(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.usage.input_tokens")["type"] = "double"
    (brk,) = diff_profiles(Profile.model_validate(old), Profile.model_validate(new))
    assert brk.subject == "gen_ai.usage.input_tokens"
    assert "int -> double" in brk.detail
