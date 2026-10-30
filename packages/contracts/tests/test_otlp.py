from typing import Any

import pytest

from traceblame_contracts.otlp import any_value, flatten_otlp_json


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"stringValue": "chat"}, "chat"),
        ({"boolValue": True}, True),
        ({"intValue": "42"}, 42),
        ({"intValue": 42}, 42),
        ({"doubleValue": 0.5}, 0.5),
        ({"doubleValue": 1}, 1.0),
        ({"bytesValue": "AQI="}, "AQI="),
        ({"arrayValue": {"values": [{"stringValue": "stop"}]}}, ["stop"]),
        ({"arrayValue": {}}, []),
        (
            {"kvlistValue": {"values": [{"key": "id", "value": {"stringValue": "doc-1"}}]}},
            {"id": "doc-1"},
        ),
        ({}, None),
    ],
)
def test_any_value(value: dict[str, Any], expected: object) -> None:
    result = any_value(value)
    assert result == expected
    assert type(result) is type(expected)


def _doc(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]
                },
                "schemaUrl": "https://opentelemetry.io/schemas/1.41.1",
                "scopeSpans": [
                    {
                        "scope": {"name": "openinference.instrumentation.openai", "version": "1"},
                        "schemaUrl": "https://opentelemetry.io/schemas/1.30.0",
                        "spans": [span],
                    }
                ],
            }
        ]
    }


_SPAN: dict[str, Any] = {
    "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
    "spanId": "00f067aa0ba902b7",
    "name": "ChatCompletion",
    "kind": 3,
    "startTimeUnixNano": "1761840000000000000",
    "endTimeUnixNano": "1761840001000000000",
    "attributes": [{"key": "llm.model_name", "value": {"stringValue": "qwen2.5:7b"}}],
    "status": {},
}


def test_flatten_maps_the_otlp_envelope() -> None:
    (span,) = flatten_otlp_json(_doc(dict(_SPAN)))
    assert span == {
        "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        "span_id": "00f067aa0ba902b7",
        "parent_span_id": "",
        "name": "ChatCompletion",
        "kind": "SPAN_KIND_CLIENT",
        "start_time_unix_nano": 1761840000000000000,
        "end_time_unix_nano": 1761840001000000000,
        "status": {"code": "STATUS_CODE_UNSET"},
        "resource": {"service.name": "svc"},
        "resource_schema_url": "https://opentelemetry.io/schemas/1.41.1",
        "scope": {
            "name": "openinference.instrumentation.openai",
            "version": "1",
            "schema_url": "https://opentelemetry.io/schemas/1.30.0",
        },
        "attributes": {"llm.model_name": "qwen2.5:7b"},
    }


def test_flatten_keeps_parent_and_error_status() -> None:
    raw = dict(_SPAN, parentSpanId="a3ce929d0e0e4736", status={"code": 2, "message": "boom"})
    (span,) = flatten_otlp_json(_doc(raw))
    assert span["parent_span_id"] == "a3ce929d0e0e4736"
    assert span["status"] == {"code": "STATUS_CODE_ERROR", "message": "boom"}


def test_flatten_accepts_enum_names() -> None:
    raw = dict(_SPAN, kind="SPAN_KIND_INTERNAL", status={"code": "STATUS_CODE_OK"})
    (span,) = flatten_otlp_json(_doc(raw))
    assert span["kind"] == "SPAN_KIND_INTERNAL"
    assert span["status"] == {"code": "STATUS_CODE_OK"}


def test_flatten_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        flatten_otlp_json(_doc(dict(_SPAN, kind=9)))


def test_flatten_omits_empty_schema_urls() -> None:
    doc: dict[str, Any] = {
        "resourceSpans": [{"scopeSpans": [{"scope": {}, "spans": [dict(_SPAN)]}]}]
    }
    (span,) = flatten_otlp_json(doc)
    assert "resource_schema_url" not in span
    assert span["scope"] == {"name": ""}
    assert span["resource"] == {}


def test_flatten_returns_every_span() -> None:
    doc = _doc(dict(_SPAN))
    second = dict(_SPAN, spanId="a3ce929d0e0e4736")
    doc["resourceSpans"][0]["scopeSpans"][0]["spans"].append(second)
    assert [s["span_id"] for s in flatten_otlp_json(doc)] == [
        "00f067aa0ba902b7",
        "a3ce929d0e0e4736",
    ]
