"""Flatten OTLP/JSON trace exports into the span documents the profile schema validates."""

from collections.abc import Iterable, Mapping
from typing import Any

SPAN_KINDS = (
    "SPAN_KIND_UNSPECIFIED",
    "SPAN_KIND_INTERNAL",
    "SPAN_KIND_SERVER",
    "SPAN_KIND_CLIENT",
    "SPAN_KIND_PRODUCER",
    "SPAN_KIND_CONSUMER",
)
STATUS_CODES = ("STATUS_CODE_UNSET", "STATUS_CODE_OK", "STATUS_CODE_ERROR")

type AttributeValue = (
    str | bool | int | float | list[AttributeValue] | dict[str, AttributeValue] | None
)


def any_value(value: Mapping[str, Any]) -> AttributeValue:
    """Convert an OTLP/JSON AnyValue to plain JSON (int64 arrives as a string)."""
    if "stringValue" in value:
        return str(value["stringValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "bytesValue" in value:
        return str(value["bytesValue"])
    if "arrayValue" in value:
        return [any_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return attributes(value["kvlistValue"].get("values", []))
    return None


def attributes(kvs: Iterable[Mapping[str, Any]]) -> dict[str, AttributeValue]:
    return {kv["key"]: any_value(kv.get("value", {})) for kv in kvs}


def _enum(value: object, names: tuple[str, ...], field: str) -> str:
    if isinstance(value, str) and value in names:
        return value
    if isinstance(value, int) and 0 <= value < len(names):
        return names[value]
    raise ValueError(f"unknown span {field}: {value!r}")


def flatten_otlp_json(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One flat document per span: envelope, resource, scope and attributes as plain JSON."""
    spans: list[dict[str, Any]] = []
    for resource_spans in doc.get("resourceSpans", []):
        resource = attributes(resource_spans.get("resource", {}).get("attributes", []))
        resource_schema_url = resource_spans.get("schemaUrl")
        for scope_spans in resource_spans.get("scopeSpans", []):
            scope_info = scope_spans.get("scope", {})
            scope: dict[str, Any] = {"name": scope_info.get("name", "")}
            if scope_info.get("version"):
                scope["version"] = scope_info["version"]
            if scope_spans.get("schemaUrl"):
                scope["schema_url"] = scope_spans["schemaUrl"]
            for span in scope_spans.get("spans", []):
                status_in = span.get("status", {})
                status: dict[str, Any] = {
                    "code": _enum(status_in.get("code", 0), STATUS_CODES, "status code")
                }
                if status_in.get("message"):
                    status["message"] = status_in["message"]
                flat: dict[str, Any] = {
                    "trace_id": span["traceId"],
                    "span_id": span["spanId"],
                    "parent_span_id": span.get("parentSpanId", ""),
                    "name": span["name"],
                    "kind": _enum(span.get("kind", 0), SPAN_KINDS, "kind"),
                    "start_time_unix_nano": int(span["startTimeUnixNano"]),
                    "end_time_unix_nano": int(span["endTimeUnixNano"]),
                    "status": status,
                    "resource": dict(resource),
                    "scope": dict(scope),
                    "attributes": attributes(span.get("attributes", [])),
                }
                if resource_schema_url:
                    flat["resource_schema_url"] = resource_schema_url
                spans.append(flat)
    return spans
