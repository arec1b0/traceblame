"""Generate the JSON Schema (draft 2020-12) for flattened normalized spans from the profile."""

import json
from typing import Any

from traceblame_contracts.otlp import SPAN_KINDS, STATUS_CODES
from traceblame_contracts.profile import (
    OPERATION_ATTRIBUTE,
    SPAN_KIND_ATTRIBUTE,
    AttributeSpec,
    Profile,
)

DIALECT = "https://json-schema.org/draft/2020-12/schema"

_TYPES: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "int": {"type": "integer"},
    "double": {"type": "number"},
    "boolean": {"type": "boolean"},
}


def _value_schema(spec: AttributeSpec) -> dict[str, Any]:
    if spec.type.endswith("[]"):
        schema: dict[str, Any] = {"type": "array", "items": dict(_TYPES[spec.type[:-2]])}
    else:
        schema = dict(_TYPES[spec.type])
    if spec.pattern is not None:
        schema["pattern"] = spec.pattern
    if spec.enum is not None:
        schema["enum"] = list(spec.enum)
    if spec.const is not None:
        schema["const"] = spec.const
    schema["description"] = spec.brief
    return schema


def _required_everywhere(profile: Profile, spec: AttributeSpec) -> bool:
    return all(spec.level_for(kind) == "required" for kind in profile.span_kinds)


def _attributes_object(profile: Profile, scope: str) -> dict[str, Any]:
    specs = profile.scoped("resource" if scope == "resource" else "span")
    properties: dict[str, Any] = {spec.name: _value_schema(spec) for spec in specs}
    for forbidden in profile.forbidden:
        # `not: {}` rather than `false`: jsonschema keeps the property in the error path.
        properties[forbidden.name] = {"not": {}, "description": f"Forbidden: {forbidden.reason}"}
    known_aegis = sorted(spec.name for spec in specs if spec.is_aegis)
    obj: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "propertyNames": {"anyOf": [{"not": {"pattern": r"^aegis\."}}, {"enum": known_aegis}]},
    }
    required = sorted(spec.name for spec in specs if _required_everywhere(profile, spec))
    if required:
        obj["required"] = required
    return obj


def _kind_rules(profile: Profile) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for kind, kind_spec in profile.span_kinds.items():
        then: dict[str, Any] = {}
        required = sorted(
            spec.name
            for spec in profile.scoped("span")
            if spec.level_for(kind) == "required" and not _required_everywhere(profile, spec)
        )
        if required:
            then["required"] = required
        if kind_spec.operations:
            then["properties"] = {OPERATION_ATTRIBUTE: {"enum": list(kind_spec.operations)}}
        if then:
            rules.append(
                {
                    "if": {
                        "properties": {
                            "attributes": {
                                "properties": {SPAN_KIND_ATTRIBUTE: {"const": kind}},
                                "required": [SPAN_KIND_ATTRIBUTE],
                            }
                        },
                        "required": ["attributes"],
                    },
                    "then": {"properties": {"attributes": then}},
                }
            )
    return rules


def generate_json_schema(profile: Profile) -> dict[str, Any]:
    hex_id = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
    return {
        "$schema": DIALECT,
        "$id": f"urn:aegis:schema:{profile.profile}:{profile.major}",
        "title": f"{profile.id} normalized span",
        "description": (
            f"Generated from the {profile.id} profile, revision {profile.revision}, on OTel "
            f"semantic conventions {profile.semconv.version}. Do not edit: run "
            "`traceblame-contracts gen-schema`."
        ),
        "x-aegis-profile": {
            "id": profile.id,
            "revision": profile.revision,
            "semconv_version": profile.semconv.version,
            "semconv_schema_url": profile.semconv.schema_url,
        },
        "type": "object",
        "required": [
            "trace_id",
            "span_id",
            "name",
            "kind",
            "start_time_unix_nano",
            "end_time_unix_nano",
            "status",
            "resource",
            "scope",
            "attributes",
        ],
        "properties": {
            "trace_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "span_id": hex_id,
            "parent_span_id": {"type": "string", "pattern": "^([0-9a-f]{16})?$"},
            "name": {"type": "string", "minLength": 1},
            "kind": {"enum": list(SPAN_KINDS)},
            "start_time_unix_nano": {"type": "integer", "minimum": 0},
            "end_time_unix_nano": {"type": "integer", "minimum": 0},
            "status": {
                "type": "object",
                "required": ["code"],
                "properties": {"code": {"enum": list(STATUS_CODES)}, "message": {"type": "string"}},
                "additionalProperties": False,
            },
            "resource": _attributes_object(profile, "resource"),
            "resource_schema_url": {"type": "string"},
            "scope": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "schema_url": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "attributes": _attributes_object(profile, "span"),
        },
        "additionalProperties": False,
        "allOf": _kind_rules(profile),
    }


def render_json_schema(profile: Profile) -> str:
    """Deterministic text form: sorted keys, two-space indent, LF, trailing newline."""
    return json.dumps(generate_json_schema(profile), indent=2, sort_keys=True) + "\n"
