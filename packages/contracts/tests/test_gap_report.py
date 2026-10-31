import copy
import json
from pathlib import Path
from typing import Any

from traceblame_contracts.gap_report import (
    FixtureGaps,
    fixture_gaps,
    missing_attributes,
    render_json,
    render_markdown,
)
from traceblame_contracts.jsonschema_gen import generate_json_schema
from traceblame_contracts.normalizers import IdentityNormalizer
from traceblame_contracts.profile import Profile, load_profile

ROOT_PROFILE = (
    Path(__file__).resolve().parents[3] / "schemas" / "profile" / "aegis-telemetry.v1.yaml"
)


def _profile(doc: dict[str, Any], extra: list[dict[str, Any]] | None = None) -> Profile:
    doc = copy.deepcopy(doc)
    doc["attributes"] += extra or []
    return Profile.model_validate(doc)


_ERROR_TYPE = {
    "name": "error.type",
    "scope": "span",
    "type": "string",
    "source": {"semconv": "error.type"},
    "requirement": {"all": "conditionally_required"},
    "condition": "when the span status is ERROR",
    "brief": "Error class.",
}


def test_missing_attributes_lists_recommended_and_conditional_ones(
    minimal_profile: dict[str, Any], llm_span: dict[str, Any]
) -> None:
    profile = _profile(minimal_profile)
    assert missing_attributes(llm_span, profile) == [
        "gen_ai.usage.input_tokens (conditionally_required: when the provider reports it)"
    ]
    llm_span["attributes"]["gen_ai.usage.input_tokens"] = 3
    assert missing_attributes(llm_span, profile) == []


def test_error_type_is_missing_only_on_error_spans(
    minimal_profile: dict[str, Any], llm_span: dict[str, Any]
) -> None:
    profile = _profile(minimal_profile, [_ERROR_TYPE])
    llm_span["attributes"]["gen_ai.usage.input_tokens"] = 3
    assert missing_attributes(llm_span, profile) == []
    llm_span["status"] = {"code": "STATUS_CODE_ERROR"}
    assert missing_attributes(llm_span, profile) == [
        "error.type (conditionally_required: when the span status is ERROR)"
    ]


_HTTP_STATUS = {
    "name": "http.response.status_code",
    "scope": "span",
    "type": "int",
    "source": {"semconv": "http.response.status_code"},
    "requirement": {"llm": "recommended"},
    "brief": "HTTP status of a failed call.",
}


def test_http_status_is_missing_only_on_error_spans(
    minimal_profile: dict[str, Any], llm_span: dict[str, Any]
) -> None:
    profile = _profile(minimal_profile, [_HTTP_STATUS])
    llm_span["attributes"]["gen_ai.usage.input_tokens"] = 3
    assert missing_attributes(llm_span, profile) == []
    llm_span["status"] = {"code": "STATUS_CODE_ERROR"}
    assert missing_attributes(llm_span, profile) == ["http.response.status_code (recommended)"]


def _export(span: dict[str, Any]) -> dict[str, Any]:
    def kv(attrs: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": kv(span["resource"])},
                "scopeSpans": [
                    {
                        "scope": span["scope"],
                        "spans": [
                            {
                                "traceId": span["trace_id"],
                                "spanId": span["span_id"],
                                "name": span["name"],
                                "kind": 3,
                                "startTimeUnixNano": str(span["start_time_unix_nano"]),
                                "endTimeUnixNano": str(span["end_time_unix_nano"]),
                                "attributes": kv(span["attributes"]),
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_fixture_gaps_counts_raw_violations_and_missing_attributes(
    minimal_profile: dict[str, Any], llm_span: dict[str, Any]
) -> None:
    profile = _profile(minimal_profile)
    schema = generate_json_schema(profile)
    raw = copy.deepcopy(llm_span)
    del raw["attributes"]["aegis.span.kind"]
    gaps = fixture_gaps(
        "sdk/pkg-1/chat_basic",
        _export(raw),
        profile,
        schema,
        raw=IdentityNormalizer(),
        normalized=IdentityNormalizer(),
    )
    assert gaps.fixture == "sdk/pkg-1/chat_basic"
    assert gaps.raw_violations == {"attributes: 'aegis.span.kind' is a required property": 1}
    assert gaps.missing == {}  # no span kind, so no kind-specific requirements apply

    gaps = fixture_gaps(
        "x", _export(llm_span), profile, schema, IdentityNormalizer(), IdentityNormalizer()
    )
    assert gaps.raw_violations == {}
    assert gaps.missing == {
        "llm: gen_ai.usage.input_tokens (conditionally_required: when the provider reports it)": 1
    }


def test_renderings() -> None:
    gaps = [
        FixtureGaps(
            "a/p-1/chat_basic", {"attributes: x is required": 2}, {"llm: y (recommended)": 1}
        ),
        FixtureGaps("b/p-1/chat_basic", {}, {}),
    ]
    md = render_markdown(gaps, "aegis-telemetry/1", "1.1.0")
    assert "| `a/p-1/chat_basic` | 2 | 1 |" in md
    assert "| `b/p-1/chat_basic` | 0 | 0 |" in md
    assert "attributes: x is required (n=2)" in md
    doc = json.loads(render_json(gaps, "aegis-telemetry/1", "1.1.0"))
    assert doc["profile"] == "aegis-telemetry/1"
    assert doc["fixtures"][0]["raw_violations"] == {"attributes: x is required": 2}


def test_opt_in_eval_and_fault_markers_are_never_missing() -> None:
    """#36: the markers are opt-in, so their absence is not a gap on any span kind."""
    profile = load_profile(ROOT_PROFILE)
    for name in ("aegis.eval.run.id", "aegis.fault.injection.id"):
        spec = profile.attribute(name)
        assert spec is not None, name
        assert set(spec.requirement.values()) == {"opt_in"}
    span = {
        "status": {"code": "STATUS_CODE_UNSET"},
        "resource": {},
        "attributes": {},
    }
    for kind in profile.span_kinds:
        span["attributes"] = {"aegis.span.kind": kind}
        missing = missing_attributes(span, profile)
        assert not [m for m in missing if "aegis.eval.run.id" in m or "aegis.fault" in m]
