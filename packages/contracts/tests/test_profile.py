import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from traceblame_contracts.profile import Profile, load_profile


def _attr(profile: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in profile["attributes"] if a["name"] == name)


def test_minimal_profile_is_valid(minimal_profile: dict[str, Any]) -> None:
    profile = Profile.model_validate(minimal_profile)
    assert profile.id == "aegis-telemetry/1"
    assert profile.attribute("gen_ai.request.model") is not None
    assert profile.attribute("missing.attr") is None


def test_load_profile_validates_the_yaml(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("profile: aegis-telemetry\nmajor: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_profile(path)


def test_all_applies_to_every_kind_and_a_kind_overrides_it(
    minimal_profile: dict[str, Any],
) -> None:
    _attr(minimal_profile, "gen_ai.request.model")["requirement"] = {
        "all": "recommended",
        "llm": "required",
    }
    spec = Profile.model_validate(minimal_profile).attribute("gen_ai.request.model")
    assert spec is not None
    assert spec.level_for("llm") == "required"
    assert spec.level_for("chain") == "recommended"


def test_kind_without_requirement_has_no_level(minimal_profile: dict[str, Any]) -> None:
    spec = Profile.model_validate(minimal_profile).attribute("gen_ai.request.model")
    assert spec is not None
    assert spec.level_for("chain") is None


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("custom.tenant", {"aegis": "extension"}),
        ("aegis.request.model", {"semconv": "aegis.request.model"}),
    ],
)
def test_aegis_namespace_and_aegis_source_go_together(
    minimal_profile: dict[str, Any], name: str, source: dict[str, str]
) -> None:
    minimal_profile["attributes"].append(
        {
            "name": name,
            "scope": "span",
            "type": "string",
            "source": source,
            "requirement": {"all": "opt_in"},
            "brief": "x",
        }
    )
    with pytest.raises(ValidationError, match="aegis"):
        Profile.model_validate(minimal_profile)


def test_semconv_source_must_name_the_same_attribute(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "gen_ai.request.model")["source"] = {"semconv": "llm.model_name"}
    with pytest.raises(ValidationError, match="renam"):
        Profile.model_validate(minimal_profile)


def test_requirement_must_use_known_span_kinds(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "gen_ai.request.model")["requirement"] = {"reranker": "required"}
    with pytest.raises(ValidationError, match="reranker"):
        Profile.model_validate(minimal_profile)


def test_resource_attributes_only_use_all(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "aegis.tenant.id")["requirement"] = {"llm": "required"}
    with pytest.raises(ValidationError, match="resource"):
        Profile.model_validate(minimal_profile)


def test_conditionally_required_needs_a_condition(minimal_profile: dict[str, Any]) -> None:
    del _attr(minimal_profile, "gen_ai.usage.input_tokens")["condition"]
    with pytest.raises(ValidationError, match="condition"):
        Profile.model_validate(minimal_profile)


def test_condition_without_conditional_level_is_rejected(
    minimal_profile: dict[str, Any],
) -> None:
    _attr(minimal_profile, "gen_ai.request.model")["condition"] = "sometimes"
    with pytest.raises(ValidationError, match="condition"):
        Profile.model_validate(minimal_profile)


def test_constraints_apply_to_strings_only(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "gen_ai.usage.input_tokens")["pattern"] = "^[0-9]+$"
    with pytest.raises(ValidationError, match="string"):
        Profile.model_validate(minimal_profile)


def test_pattern_must_compile(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "gen_ai.request.model")["pattern"] = "("
    with pytest.raises(ValidationError, match="pattern"):
        Profile.model_validate(minimal_profile)


def test_duplicate_attribute_names_are_rejected(minimal_profile: dict[str, Any]) -> None:
    minimal_profile["attributes"].append(dict(_attr(minimal_profile, "gen_ai.request.model")))
    with pytest.raises(ValidationError, match="duplicate"):
        Profile.model_validate(minimal_profile)


def test_attribute_cannot_also_be_forbidden(minimal_profile: dict[str, Any]) -> None:
    minimal_profile["forbidden"].append({"name": "gen_ai.request.model", "reason": "x"})
    with pytest.raises(ValidationError, match="forbidden"):
        Profile.model_validate(minimal_profile)


def test_profile_version_const_must_match_profile_id(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "aegis.profile.version")["const"] = "aegis-telemetry/2"
    with pytest.raises(ValidationError, match=r"aegis\.profile\.version"):
        Profile.model_validate(minimal_profile)


def test_span_kind_enum_must_match_span_kinds(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "aegis.span.kind")["enum"] = ["llm"]
    with pytest.raises(ValidationError, match=r"aegis\.span\.kind"):
        Profile.model_validate(minimal_profile)


def test_operations_need_the_operation_attribute(minimal_profile: dict[str, Any]) -> None:
    minimal_profile["attributes"] = [
        a for a in minimal_profile["attributes"] if a["name"] != "gen_ai.operation.name"
    ]
    with pytest.raises(ValidationError, match=r"gen_ai\.operation\.name"):
        Profile.model_validate(minimal_profile)


def test_unknown_keys_are_rejected(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "gen_ai.request.model")["requried"] = True
    with pytest.raises(ValidationError):
        Profile.model_validate(minimal_profile)


def test_algorithm_version_is_an_optional_positive_integer(
    minimal_profile: dict[str, Any],
) -> None:
    source = _attr(minimal_profile, "aegis.span.kind")["source"]
    source |= {"algorithm": "sha256 of the arguments", "algorithm_version": 1}
    spec = Profile.model_validate(minimal_profile).attribute("aegis.span.kind")
    assert spec is not None
    assert getattr(spec.source, "algorithm_version", None) == 1


@pytest.mark.parametrize("version", [0, -1, "1", 1.5])
def test_algorithm_version_rejects_anything_but_a_positive_integer(
    minimal_profile: dict[str, Any], version: object
) -> None:
    source = _attr(minimal_profile, "aegis.span.kind")["source"]
    source |= {"algorithm": "sha256 of the arguments", "algorithm_version": version}
    with pytest.raises(ValidationError):
        Profile.model_validate(minimal_profile)


def test_algorithm_version_needs_an_algorithm(minimal_profile: dict[str, Any]) -> None:
    _attr(minimal_profile, "aegis.span.kind")["source"]["algorithm_version"] = 1
    with pytest.raises(ValidationError, match="algorithm"):
        Profile.model_validate(minimal_profile)


_ROOT_PROFILE = (
    Path(__file__).resolve().parents[3] / "schemas" / "profile" / "aegis-telemetry.v1.yaml"
)


@pytest.mark.parametrize("name", ["aegis.eval.run.id", "aegis.fault.injection.id"])
@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("01J9Z3K8WQ5Y4N2V7XHB6T0DCE", True),
        ("3f2b8c1e-6d4a-4f7e-9b2a-0c5d8e1f3a7b", True),
        ("run-2026-10-31.cuad:split=test", False),
        ("run-2026-10-31.cuad", True),
        ("a" * 128, True),
        ("a" * 129, False),
        ("", False),
        ("run 1", False),
        ("прогон", False),
        ("run\n", False),
    ],
)
def test_marker_ids_are_bounded_in_size_and_charset(name: str, value: str, ok: bool) -> None:
    """#36: markers force 100% sampling, so their values are short, plain ids."""
    spec = load_profile(_ROOT_PROFILE).attribute(name)
    assert spec is not None
    assert spec.pattern is not None
    assert (re.fullmatch(spec.pattern.removeprefix("^").removesuffix("$"), value) is not None) == ok
