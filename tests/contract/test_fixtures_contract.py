"""Golden fixtures are real, untouched, content-free exports, and the profile applies to them."""

import os
import re
from pathlib import Path
from typing import Any

import pytest

from traceblame_contracts.fixtures import Fixture, discover_fixtures
from traceblame_contracts.normalizers import (
    BINARY_ENV,
    CONFIG_ENV,
    DEFAULT_GO_BINARY,
    DEFAULT_GO_CONFIG,
    GoNormalizer,
)
from traceblame_contracts.otlp import flatten_otlp_json
from traceblame_contracts.profile import Profile
from traceblame_contracts.validate import validate_span

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = ROOT / "collector" / "testdata" / "fixtures"

pytestmark = pytest.mark.contract

FIXTURES = discover_fixtures(FIXTURES_ROOT)
SDKS = {"openinference", "openllmetry", "otel-genai"}

# Vendor attributes that carry prompt/completion content when content capture is on.
_VENDOR_CONTENT = re.compile(
    r"^((input|output)\.value"
    r"|llm\.(input|output)_messages\..*"
    r"|llm\.prompts?(\..*)?"
    r"|gen_ai\.(prompt|completion)\..*)$"
)
_REDACTED = "__REDACTED__"


def test_every_sdk_has_a_fixture() -> None:
    assert {f.provenance().sdk for f in FIXTURES} == SDKS


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_fixture_is_the_captured_export(fixture: Fixture) -> None:
    provenance = fixture.provenance()
    assert fixture.hash_matches(), "fixture was edited after capture; re-run `make fixtures`"
    assert (ROOT / provenance.capture_script).is_file()
    assert fixture.path.parent.parent.name == provenance.sdk
    assert fixture.path.name == f"{provenance.scenario}.otlp.json"
    assert fixture.path.parent.name in {f"{k}-{v}" for k, v in provenance.packages.items()}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_fixture_is_well_formed_otlp_json(fixture: Fixture) -> None:
    spans = flatten_otlp_json(fixture.load())
    assert spans
    for span in spans:
        assert re.fullmatch(r"[0-9a-f]{32}", span["trace_id"])
        assert re.fullmatch(r"[0-9a-f]{16}", span["span_id"])
        assert re.fullmatch(r"([0-9a-f]{16})?", span["parent_span_id"])
        assert span["end_time_unix_nano"] >= span["start_time_unix_nano"] > 0


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_fixture_carries_no_raw_content(fixture: Fixture, profile: Profile) -> None:
    forbidden = {f.name for f in profile.forbidden}
    leaks = [
        f"{span['span_id']}: {key}"
        for span in flatten_otlp_json(fixture.load())
        for key, value in span["attributes"].items()
        if (key in forbidden or _VENDOR_CONTENT.match(key)) and value != _REDACTED
    ]
    assert leaks == []


# The contract gate: the collector's normalizer (collector/cmd/normalize), built by
# `make contract`. Raw SDK exports (identity) go to the gap report instead.
GO = GoNormalizer(
    Path(os.environ.get(BINARY_ENV, ROOT / DEFAULT_GO_BINARY)),
    Path(os.environ.get(CONFIG_ENV, ROOT / DEFAULT_GO_CONFIG)),
)


@pytest.fixture(scope="module")
def normalized() -> dict[str, list[dict[str, Any]]]:
    return {f.id: flatten_otlp_json(GO.normalize(f.load())) for f in FIXTURES}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_normalized_fixture_conforms_to_the_profile(
    fixture: Fixture,
    normalized: dict[str, list[dict[str, Any]]],
    committed_schema: dict[str, Any],
) -> None:
    violations = [
        f"{span['span_id']} {v.path}: {v.message}"
        for span in normalized[fixture.id]
        for v in validate_span(span, committed_schema)
    ]
    assert violations == []


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_normalized_fixture_names_its_sdk_and_normalizer(
    fixture: Fixture, normalized: dict[str, list[dict[str, Any]]]
) -> None:
    for span in normalized[fixture.id]:
        assert span["attributes"]["aegis.source.sdk"] == fixture.provenance().sdk
        assert re.fullmatch(r"\d+\.\d+\.\d+", span["attributes"]["aegis.normalizer.version"])
