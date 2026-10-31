import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceblame_contracts.compat import ProfileDiff, breaking_within_major, diff_profiles
from traceblame_contracts.profile import Profile
from traceblame_contracts.vectors import Registry, VectorFile

_OPTIONAL: dict[str, Any] = {
    "name": "gen_ai.response.model",
    "scope": "span",
    "type": "string",
    "source": {"semconv": "gen_ai.response.model"},
    "requirement": {"llm": "recommended"},
    "brief": "Response model.",
}


def _attr(profile: dict[str, Any], name: str) -> dict[str, Any]:
    return next(a for a in profile["attributes"] if a["name"] == name)


def _without(profile: dict[str, Any], name: str) -> None:
    profile["attributes"] = [a for a in profile["attributes"] if a["name"] != name]


def _diff(old: dict[str, Any], new: dict[str, Any]) -> ProfileDiff:
    return diff_profiles(Profile.model_validate(old), Profile.model_validate(new))


def _changes(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, str]]:
    return [(c.severity, c.rule) for c in _diff(old, new).changes]


def _algorithm(profile: dict[str, Any], text: str | None) -> None:
    """Set (or with None remove) the source.algorithm of aegis.span.kind, the carrier here."""
    source = _attr(profile, "aegis.span.kind")["source"]
    if text is None:
        source.pop("algorithm", None)
    else:
        source["algorithm"] = text


# args_hash's algorithm text in profile 1.1.0 and 1.2.0, as folded by the YAML loader.
_ARGS_HASH_1_1_0 = "first 16 hex chars of sha256 over the canonical JSON of the arguments"
_ARGS_HASH_1_2_0 = (
    "first 16 lowercase hex chars of SHA-256 over the RFC 8785 (JCS) serialization of the "
    "arguments; over their raw UTF-8 bytes when they are not I-JSON"
)


def _repin(profile: dict[str, Any], version: str) -> None:
    profile["semconv"]["version"] = version
    profile["semconv"]["commit"] = "f" * 40
    profile["semconv"]["schema_url"] = f"https://opentelemetry.io/schemas/{version}"


@pytest.fixture
def old(minimal_profile: dict[str, Any]) -> dict[str, Any]:
    return minimal_profile


@pytest.fixture
def new(minimal_profile: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(minimal_profile)


def test_identical_profiles_have_no_changes(old: dict[str, Any], new: dict[str, Any]) -> None:
    diff = _diff(old, new)
    assert diff.changes == ()
    assert diff.bump == "none"


# Major: needs the next profile major.


def test_required_attribute_removed_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _without(new, "gen_ai.request.model")
    assert _changes(old, new) == [("major", "attribute_removed")]
    assert _diff(old, new).bump == "major"


def test_required_attribute_renamed_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    spec = _attr(new, "gen_ai.request.model")
    spec["name"] = spec["source"]["semconv"] = "gen_ai.request.model_id"
    assert _changes(old, new) == [
        ("major", "attribute_removed"),
        ("major", "required_attribute_added"),
    ]


def test_required_attribute_retyped_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["type"] = "int"
    assert _changes(old, new) == [("major", "type_changed")]


def test_enum_value_removed_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    old["span_kinds"]["tool"] = {"description": "Tool call.", "operations": []}
    _attr(old, "aegis.span.kind")["enum"] = ["llm", "chain", "tool"]
    assert _changes(old, new) == [
        ("major", "span_kind_removed"),
        ("major", "enum_value_removed"),
    ]


def test_optional_attribute_removed_is_major_too(old: dict[str, Any], new: dict[str, Any]) -> None:
    old["attributes"].append(copy.deepcopy(_OPTIONAL))
    assert _changes(old, new) == [("major", "attribute_removed")]


def test_attribute_moved_between_scopes_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "aegis.tenant.id")["scope"] = "span"
    assert _changes(old, new) == [("major", "scope_changed")]


def test_tightened_requirement_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    spec = _attr(new, "gen_ai.usage.input_tokens")
    spec["requirement"] = {"llm": "required"}
    del spec["condition"]
    assert _changes(old, new) == [("major", "requirement_tightened")]


def test_new_required_attribute_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["attributes"].append(dict(_OPTIONAL, requirement={"llm": "required"}))
    assert _changes(old, new) == [("major", "required_attribute_added")]


def test_removed_operation_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    old["span_kinds"]["llm"]["operations"] = ["chat", "text_completion"]
    assert _changes(old, new) == [("major", "operation_removed")]


def test_constraining_operations_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["span_kinds"]["chain"]["operations"] = ["invoke_workflow"]
    assert _changes(old, new) == [("major", "operations_constrained")]


def test_new_pattern_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["pattern"] = "^[a-z]+$"
    assert _changes(old, new) == [("major", "constraint_changed")]


def test_new_enum_constraint_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["enum"] = ["qwen2.5:7b"]
    assert _changes(old, new) == [("major", "constraint_changed")]


def test_new_forbidden_attribute_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["forbidden"].append({"name": "gen_ai.output.messages", "reason": "content"})
    assert _changes(old, new) == [("major", "forbidden_added")]


def test_added_algorithm_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _algorithm(new, _ARGS_HASH_1_1_0)
    assert _changes(old, new) == [("major", "algorithm_added")]
    assert _diff(old, new).bump == "major"


def test_removed_algorithm_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _algorithm(old, _ARGS_HASH_1_1_0)
    assert _changes(old, new) == [("major", "algorithm_removed")]


def test_changed_algorithm_text_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _algorithm(old, "first 16 hex chars of sha256")
    _algorithm(new, "first 12 hex chars of sha256")
    (change,) = _diff(old, new).changes
    assert (change.severity, change.rule) == ("major", "algorithm_changed")
    assert change.subject == "aegis.span.kind"
    assert change.detail == "first 16 hex chars of sha256 -> first 12 hex chars of sha256"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("sha256 of the arguments", "SHA256 of the arguments"),
        ("sha256 of the arguments", "sha256 of the arguments."),
        ("first 16 hex chars", "first 16 lowercase hex chars"),
    ],
    ids=["case", "punctuation", "word"],
)
def test_any_wording_change_of_the_algorithm_is_major(
    old: dict[str, Any], new: dict[str, Any], before: str, after: str
) -> None:
    _algorithm(old, before)
    _algorithm(new, after)
    assert _changes(old, new) == [("major", "algorithm_changed")]


def test_args_hash_rewording_of_profile_1_2_0_is_major(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    """The 1.1.0 -> 1.2.0 change (#9) would have needed aegis-telemetry/2 under this rule."""
    _algorithm(old, _ARGS_HASH_1_1_0)
    _algorithm(new, _ARGS_HASH_1_2_0)
    assert _changes(old, new) == [("major", "algorithm_changed")]
    assert breaking_within_major(Profile.model_validate(old), Profile.model_validate(new))


def test_algorithm_change_with_a_major_bump_passes(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _algorithm(old, _ARGS_HASH_1_1_0)
    _algorithm(new, _ARGS_HASH_1_2_0)
    new["major"] = 2
    _attr(new, "aegis.profile.version")["const"] = "aegis-telemetry/2"
    assert breaking_within_major(Profile.model_validate(old), Profile.model_validate(new)) == []


# Minor: allowed within the major.


def test_optional_attribute_added_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["attributes"].append(copy.deepcopy(_OPTIONAL))
    assert _changes(old, new) == [("minor", "attribute_added")]
    assert _diff(old, new).bump == "minor"


def test_pin_change_with_identical_attributes_is_minor(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    """v1.41.1 -> a hypothetical v1.41.2 that defines the same attributes."""
    _repin(new, "1.41.2")
    diff = _diff(old, new)
    assert _changes(old, new) == [("minor", "semconv_pin_changed")]
    assert diff.bump == "minor"
    assert "1.41.1 -> 1.41.2" in diff.changes[0].detail


def test_pin_change_with_a_retyped_attribute_is_major(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _repin(new, "1.41.2")
    _attr(new, "gen_ai.usage.input_tokens")["type"] = "double"
    assert _changes(old, new) == [("minor", "semconv_pin_changed"), ("major", "type_changed")]
    assert _diff(old, new).bump == "major"


def test_loosened_requirement_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["requirement"] = {"llm": "recommended"}
    assert _changes(old, new) == [("minor", "requirement_loosened")]


def test_added_enum_value_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["span_kinds"]["tool"] = {"description": "Tool call.", "operations": []}
    _attr(new, "aegis.span.kind")["enum"] = ["llm", "chain", "tool"]
    assert _changes(old, new) == [("minor", "span_kind_added"), ("minor", "enum_value_added")]


def test_added_operation_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["span_kinds"]["llm"]["operations"] = ["chat", "text_completion"]
    assert _changes(old, new) == [("minor", "operation_added")]


def test_removed_pattern_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(old, "gen_ai.request.model")["pattern"] = "^[a-z]+$"
    assert _changes(old, new) == [("minor", "constraint_removed")]


def test_removed_enum_constraint_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(old, "gen_ai.request.model")["enum"] = ["qwen2.5:7b"]
    assert _changes(old, new) == [("minor", "constraint_removed")]


def test_unconstrained_operations_are_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["span_kinds"]["llm"]["operations"] = []
    assert _changes(old, new) == [("minor", "operations_unconstrained")]


def test_pin_moving_to_the_genai_repository_is_minor(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _repin(new, "1.42.0")
    new["semconv"]["repository"] = "open-telemetry/semantic-conventions-genai"
    (change,) = _diff(old, new).changes
    assert (change.severity, change.rule) == ("minor", "semconv_pin_changed")
    assert change.detail == (
        "1.41.1 -> 1.42.0 (open-telemetry/semantic-conventions -> "
        "open-telemetry/semantic-conventions-genai)"
    )


@pytest.mark.parametrize(
    "after",
    [
        "first 16 hex chars  of sha256\n  over the canonical JSON",
        "  first 16 hex chars of sha256 over the\tcanonical JSON\n",
        "first 16 hex chars of sha256\nover the\ncanonical JSON",
    ],
    ids=["double-space-and-wrap", "padding-and-tab", "one-word-per-line"],
)
def test_algorithm_whitespace_and_wrapping_are_not_changes(
    old: dict[str, Any], new: dict[str, Any], after: str
) -> None:
    _algorithm(old, "first 16 hex chars of sha256 over the canonical JSON")
    _algorithm(new, after)
    assert _diff(old, new).changes == ()
    assert _diff(old, new).bump == "none"


def test_brief_rewording_is_not_a_change(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.request.model")["brief"] = "A different description."
    assert _diff(old, new).changes == ()


def test_new_attribute_with_an_algorithm_is_not_an_algorithm_change(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    spec = {
        "name": "aegis.tool.args_hash",
        "scope": "span",
        "type": "string",
        "source": {"aegis": "extension", "algorithm": _ARGS_HASH_1_2_0},
        "requirement": {"llm": "recommended"},
        "brief": "Hash.",
    }
    new["attributes"].append(spec)
    assert _changes(old, new) == [("minor", "attribute_added")]


def test_removed_forbidden_attribute_is_minor(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["forbidden"] = []
    assert _changes(old, new) == [("minor", "forbidden_removed")]


# The CI gate: major changes are rejected only when the major stays the same.


def test_major_change_within_the_same_major_is_rejected(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _without(new, "gen_ai.request.model")
    (change,) = breaking_within_major(Profile.model_validate(old), Profile.model_validate(new))
    assert change.rule == "attribute_removed"


def test_major_change_with_a_major_bump_passes(old: dict[str, Any], new: dict[str, Any]) -> None:
    new["major"] = 2
    _attr(new, "aegis.profile.version")["const"] = "aegis-telemetry/2"
    _without(new, "gen_ai.request.model")
    assert _diff(old, new).bump == "major"
    assert breaking_within_major(Profile.model_validate(old), Profile.model_validate(new)) == []


def test_minor_change_passes_the_gate(old: dict[str, Any], new: dict[str, Any]) -> None:
    _repin(new, "1.41.2")
    assert breaking_within_major(Profile.model_validate(old), Profile.model_validate(new)) == []


def test_change_names_its_subject_and_detail(old: dict[str, Any], new: dict[str, Any]) -> None:
    _attr(new, "gen_ai.usage.input_tokens")["type"] = "double"
    (change,) = _diff(old, new).changes
    assert change.subject == "gen_ai.usage.input_tokens"
    assert change.detail == "int -> double"


# algorithm_version (ADR-0002, section 5, issue #32).

_CARRIER = "aegis.span.kind"
_TEXT = "sha256 over the key-sorted arguments"
_REWORDED = "the SHA-256 of the arguments, keys sorted"


def _versioned(profile: dict[str, Any], text: str | None, version: int | None) -> None:
    _algorithm(profile, text)
    source = _attr(profile, _CARRIER)["source"]
    if version is None:
        source.pop("algorithm_version", None)
    else:
        source["algorithm_version"] = version


def _vector(vector_id: str, expected: str = "0123456789abcdef") -> dict[str, Any]:
    return {"id": vector_id, "input": f"in-{vector_id}", "canonical": None, "expected": expected}


def _registry(*vectors: dict[str, Any], version: int = 1) -> Registry:
    doc = {"attribute": _CARRIER, "algorithm_version": version, "vectors": list(vectors)}
    return {_CARRIER: VectorFile.model_validate(doc)}


def _classified(
    old: dict[str, Any],
    new: dict[str, Any],
    old_vectors: Registry | None = None,
    new_vectors: Registry | None = None,
) -> list[tuple[str, str]]:
    diff = diff_profiles(
        Profile.model_validate(old),
        Profile.model_validate(new),
        old_vectors=old_vectors,
        new_vectors=new_vectors,
    )
    return [(c.severity, c.rule) for c in diff.changes]


def test_adopting_version_1_with_the_text_unchanged_is_minor(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, None)
    _versioned(new, _TEXT, 1)
    assert _classified(old, new) == [("minor", "algorithm_version_adopted")]
    assert not breaking_within_major(Profile.model_validate(old), Profile.model_validate(new))


def test_adopting_version_1_ignores_whitespace_in_the_text(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, None)
    _versioned(new, "sha256 over\n  the key-sorted   arguments", 1)
    assert _classified(old, new) == [("minor", "algorithm_version_adopted")]


def test_adopting_version_1_together_with_a_text_change_is_major(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, None)
    _versioned(new, _REWORDED, 1)
    assert _classified(old, new) == [("major", "algorithm_version_adopted_with_text_change")]


def test_adopting_it_with_a_text_change_is_major_even_with_registered_vectors(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, None)
    _versioned(new, _REWORDED, 1)
    vectors = _registry(_vector("a"))
    assert _classified(old, new, vectors, vectors) == [
        ("major", "algorithm_version_adopted_with_text_change")
    ]


@pytest.mark.parametrize("version", [2, 5])
def test_introducing_a_version_above_1_is_major(
    old: dict[str, Any], new: dict[str, Any], version: int
) -> None:
    _versioned(old, _TEXT, None)
    _versioned(new, _TEXT, version)
    assert _classified(old, new) == [("major", "algorithm_version_introduced_above_1")]


def test_removing_the_version_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, None)
    assert _classified(old, new) == [("major", "algorithm_version_removed")]


def test_lowering_the_version_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 2)
    _versioned(new, _TEXT, 1)
    assert _classified(old, new) == [("major", "algorithm_version_lowered")]


def test_raising_the_version_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, 2)
    assert _classified(old, new) == [("major", "algorithm_version_raised")]


def test_raising_the_version_with_a_text_change_is_one_major_change(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 2)
    assert _classified(old, new) == [("major", "algorithm_version_raised")]


def test_algorithm_added_or_removed_stays_major_whatever_the_version(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(new, _TEXT, 1)
    assert _classified(old, new) == [("major", "algorithm_added")]
    assert _classified(new, old) == [("major", "algorithm_removed")]


def test_a_reword_with_unchanged_base_vectors_is_minor(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 1)
    base = _registry(_vector("a"), _vector("b"))
    assert _classified(old, new, base, base) == [("minor", "algorithm_reworded")]
    assert not breaking_within_major(
        Profile.model_validate(old),
        Profile.model_validate(new),
        old_vectors=base,
        new_vectors=base,
    )


def test_a_reword_may_add_vectors(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 1)
    head = _registry(_vector("a"), _vector("b"), _vector("c"))
    assert _classified(old, new, _registry(_vector("a"), _vector("b")), head) == [
        ("minor", "algorithm_reworded")
    ]


def test_a_reword_without_any_registered_vectors_is_major(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 1)
    assert _classified(old, new) == [("major", "algorithm_changed")]
    assert _classified(old, new, {}, {}) == [("major", "algorithm_changed")]


def test_a_reword_with_vectors_only_at_head_is_major(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    """Vectors added in the rewording PR prove nothing about the old computation."""
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 1)
    (change,) = diff_profiles(
        Profile.model_validate(old),
        Profile.model_validate(new),
        old_vectors={},
        new_vectors=_registry(_vector("a")),
    ).changes
    assert (change.severity, change.rule) == ("major", "algorithm_changed")
    assert "base" in change.detail


def test_a_reword_with_a_changed_vector_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 1)
    base = _registry(_vector("a"), _vector("b"))
    head = _registry(_vector("a"), _vector("b", expected="fedcba9876543210"))
    assert _classified(old, new, base, head) == [
        ("major", "algorithm_changed"),
        ("major", "vector_changed"),
    ]


def test_a_reword_with_a_removed_vector_is_major(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _REWORDED, 1)
    base = _registry(_vector("a"), _vector("b"))
    assert _classified(old, new, base, _registry(_vector("a"))) == [
        ("major", "algorithm_changed"),
        ("major", "vector_removed"),
    ]


def test_a_reword_of_an_unversioned_algorithm_stays_major_even_with_vectors(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, None)
    _versioned(new, _REWORDED, None)
    base = _registry(_vector("a"))
    assert _classified(old, new, base, base) == [("major", "algorithm_changed")]


def test_a_changed_vector_is_major_even_with_the_text_unchanged(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, 1)
    head = _registry(_vector("a", expected="fedcba9876543210"))
    assert _classified(old, new, _registry(_vector("a")), head) == [("major", "vector_changed")]
    assert breaking_within_major(
        Profile.model_validate(old),
        Profile.model_validate(new),
        old_vectors=_registry(_vector("a")),
        new_vectors=head,
    )


@pytest.mark.parametrize("field", ["input", "canonical", "expected"])
def test_every_vector_field_is_covered_by_the_changed_rule(
    old: dict[str, Any], new: dict[str, Any], field: str
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, 1)
    changed = _vector("a") | {field: "changed"}
    assert _classified(old, new, _registry(_vector("a")), _registry(changed)) == [
        ("major", "vector_changed")
    ]


def test_a_removed_vector_is_major_even_with_the_text_unchanged(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, 1)
    base = _registry(_vector("a"), _vector("b"))
    assert _classified(old, new, base, _registry(_vector("a"))) == [("major", "vector_removed")]


def test_a_vanished_registry_file_removes_every_vector(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, 1)
    assert _classified(old, new, _registry(_vector("a"), _vector("b")), {}) == [
        ("major", "vector_removed"),
        ("major", "vector_removed"),
    ]


def test_adding_vectors_is_not_a_change(old: dict[str, Any], new: dict[str, Any]) -> None:
    _versioned(old, _TEXT, 1)
    _versioned(new, _TEXT, 1)
    diff = diff_profiles(
        Profile.model_validate(old),
        Profile.model_validate(new),
        old_vectors=_registry(_vector("a")),
        new_vectors=_registry(_vector("a"), _vector("b")),
    )
    assert diff.changes == ()
    assert diff.bump == "none"


def test_vectors_of_a_removed_attribute_do_not_add_changes(
    old: dict[str, Any], new: dict[str, Any]
) -> None:
    """The removal is the change (major, as in #28); its vectors need not stay."""
    old["attributes"].append(
        {
            "name": "aegis.tool.args_hash",
            "scope": "span",
            "type": "string",
            "source": {"aegis": "extension", "algorithm": _TEXT, "algorithm_version": 1},
            "requirement": {"llm": "recommended"},
            "brief": "Hash.",
        }
    )
    doc = {
        "attribute": "aegis.tool.args_hash",
        "algorithm_version": 1,
        "vectors": [_vector("a")],
    }
    vectors = {"aegis.tool.args_hash": VectorFile.model_validate(doc)}
    assert _classified(old, new, vectors, {}) == [("major", "attribute_removed")]


def test_adopting_version_1_on_the_committed_args_hash_is_minor() -> None:
    """The real profile against itself with the field removed: profile 1.3.0 -> 1.4.0."""
    path = Path(__file__).resolve().parents[3] / "schemas" / "profile" / "aegis-telemetry.v1.yaml"
    head = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = copy.deepcopy(head)
    source = _attr(base, "aegis.tool.args_hash")["source"]
    assert source.pop("algorithm_version") == 1
    assert _classified(base, head) == [("minor", "algorithm_version_adopted")]
    assert _diff(base, head).bump == "minor"


def test_the_eval_and_fault_markers_are_two_optional_additions() -> None:
    """#36 on the real profile: 1.4.0 -> 1.5.0 adds two opt-in attributes, nothing else."""
    path = Path(__file__).resolve().parents[3] / "schemas" / "profile" / "aegis-telemetry.v1.yaml"
    head = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = copy.deepcopy(head)
    for name in ("aegis.eval.run.id", "aegis.fault.injection.id"):
        _without(base, name)
    assert _classified(base, head) == [
        ("minor", "attribute_added"),
        ("minor", "attribute_added"),
    ]
    assert [c.subject for c in _diff(base, head).changes] == [
        "aegis.eval.run.id",
        "aegis.fault.injection.id",
    ]
