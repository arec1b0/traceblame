"""The trace.completed event and its JSON Schema (ADR-0003, #35)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schemas" / "events" / "trace.completed.v1.schema.json"
GOLDEN = ROOT / "services" / "assembler" / "testdata" / "trace_completed.golden.json"

pytestmark = pytest.mark.contract


def _schema() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(SCHEMA.read_text(encoding="utf-8")))


def _golden() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(GOLDEN.read_text(encoding="utf-8")))


def _errors(payload: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(_schema())
    errors: Iterator[ValidationError] = validator.iter_errors(payload)  # pyright: ignore[reportUnknownMemberType]  # jsonschema's stubs leave iter_errors partially unknown
    return [e.message for e in errors]


def test_the_schema_is_a_valid_json_schema() -> None:
    Draft202012Validator.check_schema(_schema())


def test_the_event_has_exactly_the_issue_fields() -> None:
    schema = _schema()
    assert set(schema["properties"]) == {"trace_id", "revision", "span_count", "assembler_version"}
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_the_assemblers_golden_payload_validates() -> None:
    assert _errors(_golden()) == []


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"trace_id": "4BF92F3577B34DA6A3CE929D0E0E4736"}, "does not match"),
        ({"trace_id": "4bf92f35"}, "does not match"),
        ({"revision": "1793700000123"}, "is not of type 'integer'"),
        ({"revision": -1}, "less than the minimum"),
        ({"revision": 1.5}, "is not of type 'integer'"),
        ({"span_count": 0}, "less than the minimum"),
        ({"assembler_version": "v1"}, "does not match"),
        ({"tenant_id": "tenant-eval"}, "Additional properties"),
    ],
)
def test_a_payload_that_breaks_the_contract_is_rejected(
    change: dict[str, object], message: str
) -> None:
    errors = _errors({**_golden(), **change})
    assert any(message in e for e in errors), errors


@pytest.mark.parametrize("field", ["trace_id", "revision", "span_count", "assembler_version"])
def test_every_field_is_required(field: str) -> None:
    payload = _golden()
    del payload[field]
    assert any(field in e for e in _errors(payload))
