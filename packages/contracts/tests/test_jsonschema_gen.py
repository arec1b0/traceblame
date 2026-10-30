import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from traceblame_contracts.jsonschema_gen import generate_json_schema, render_json_schema
from traceblame_contracts.profile import Profile
from traceblame_contracts.validate import validate_span


@pytest.fixture
def schema(minimal_profile: dict[str, Any]) -> dict[str, Any]:
    return generate_json_schema(Profile.model_validate(minimal_profile))


def _messages(span: dict[str, Any], schema: dict[str, Any]) -> str:
    return "\n".join(f"{v.path}: {v.message}" for v in validate_span(span, schema))


def test_rendering_is_deterministic_and_ends_with_newline(
    minimal_profile: dict[str, Any],
) -> None:
    profile = Profile.model_validate(minimal_profile)
    first = render_json_schema(profile)
    assert first == render_json_schema(profile)
    assert first.endswith("}\n")
    assert json.loads(first) == generate_json_schema(profile)


def test_schema_is_valid_draft_2020_12(schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:aegis:schema:aegis-telemetry:1"


def test_valid_llm_span_passes(llm_span: dict[str, Any], schema: dict[str, Any]) -> None:
    assert validate_span(llm_span, schema) == []


def test_attribute_required_for_the_span_kind_is_enforced(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    del llm_span["attributes"]["gen_ai.request.model"]
    assert "'gen_ai.request.model' is a required property" in _messages(llm_span, schema)


def test_attribute_required_for_another_kind_is_not_enforced(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    llm_span["attributes"] = {
        "aegis.profile.version": "aegis-telemetry/1",
        "aegis.span.kind": "chain",
    }
    assert validate_span(llm_span, schema) == []


def test_attribute_required_for_every_kind_is_enforced(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    del llm_span["attributes"]["aegis.profile.version"]
    assert "'aegis.profile.version' is a required property" in _messages(llm_span, schema)


def test_required_resource_attribute_is_enforced(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    del llm_span["resource"]["aegis.tenant.id"]
    assert "resource: 'aegis.tenant.id' is a required property" in _messages(llm_span, schema)


def test_operation_must_belong_to_the_span_kind(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    llm_span["attributes"]["gen_ai.operation.name"] = "execute_tool"
    assert "attributes/gen_ai.operation.name" in _messages(llm_span, schema)


def test_unknown_aegis_attribute_is_rejected(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    llm_span["attributes"]["aegis.custom"] = "x"
    assert "aegis.custom" in _messages(llm_span, schema)


def test_aegis_attribute_in_the_wrong_scope_is_rejected(
    llm_span: dict[str, Any], schema: dict[str, Any]
) -> None:
    llm_span["attributes"]["aegis.tenant.id"] = "tenant-a"
    assert "aegis.tenant.id" in _messages(llm_span, schema)


def test_vendor_attributes_pass_through(llm_span: dict[str, Any], schema: dict[str, Any]) -> None:
    llm_span["attributes"]["llm.model_name"] = "qwen2.5:7b"
    llm_span["resource"]["telemetry.sdk.language"] = "python"
    assert validate_span(llm_span, schema) == []


def test_forbidden_attribute_is_rejected(llm_span: dict[str, Any], schema: dict[str, Any]) -> None:
    llm_span["attributes"]["gen_ai.input.messages"] = "[]"
    assert "gen_ai.input.messages" in _messages(llm_span, schema)


def test_attribute_type_is_enforced(llm_span: dict[str, Any], schema: dict[str, Any]) -> None:
    llm_span["attributes"]["gen_ai.usage.input_tokens"] = "12"
    assert "attributes/gen_ai.usage.input_tokens" in _messages(llm_span, schema)


def test_profile_version_must_match(llm_span: dict[str, Any], schema: dict[str, Any]) -> None:
    llm_span["attributes"]["aegis.profile.version"] = "aegis-telemetry/0"
    assert "attributes/aegis.profile.version" in _messages(llm_span, schema)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trace_id", "4BF92F3577B34DA6A3CE929D0E0E4736"),
        ("span_id", "00f067aa"),
        ("parent_span_id", "xyz"),
        ("kind", "CLIENT"),
        ("start_time_unix_nano", -1),
    ],
)
def test_envelope_fields_are_checked(
    llm_span: dict[str, Any], schema: dict[str, Any], field: str, value: object
) -> None:
    llm_span[field] = value
    assert field in _messages(llm_span, schema)


def test_status_code_is_checked(llm_span: dict[str, Any], schema: dict[str, Any]) -> None:
    llm_span["status"] = {"code": 2}
    assert "status/code" in _messages(llm_span, schema)
