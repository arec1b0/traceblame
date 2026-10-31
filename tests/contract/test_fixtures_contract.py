"""Golden fixtures are real, untouched, content-free exports, and the profile applies to them."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

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

MAPPINGS = ROOT / "collector" / "processor" / "normalizer" / "mappings"
_REDACTED = "__REDACTED__"
# Words of the capture prompts and answers (tools/fixtures/capture.py): what content capture
# puts into the *_content fixtures, and nothing else in a fixture contains.
_NEEDLES = ("France", "Paris", "weather")


def _content_rules(profile: Profile) -> tuple[set[str], list[re.Pattern[str]]]:
    """The normalizer's content rules: the profile's forbidden list plus every table's."""
    names = {f.name for f in profile.forbidden}
    patterns: list[re.Pattern[str]] = []
    for path in sorted(MAPPINGS.rglob("*.yaml")):
        table: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries: list[str | dict[str, str]] = table.get("content", [])
        for entry in entries:
            if isinstance(entry, str):
                names.add(entry)
            else:
                patterns.append(re.compile(entry["pattern"]))
    return names, patterns


def _content_attributes(spans: list[dict[str, Any]], profile: Profile) -> list[str]:
    names, patterns = _content_rules(profile)
    return [
        f"{span['span_id']}: {key}"
        for span in spans
        for holder in (span["resource"], span["attributes"])
        for key, value in holder.items()
        if (key in names or any(p.search(key) for p in patterns)) and value != _REDACTED
    ]


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
def test_fixture_carries_content_only_when_captured_with_it(fixture: Fixture) -> None:
    """Content-off exports hold no prompt or completion text; content-on ones must, or the
    redaction tests prove nothing. Judged by the text itself, not by attribute names: the
    normalizer also drops metadata attributes by policy (e.g. llm.invocation_parameters)."""
    text = fixture.path.read_text(encoding="utf-8")
    found = [n for n in _NEEDLES if n in text]
    if fixture.provenance().content_capture:
        assert found, "a content_capture fixture without content proves nothing"
        assert fixture.id.endswith("_content")
    else:
        assert found == []
        assert not fixture.id.endswith("_content")


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


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_normalized_fixture_carries_no_content(
    fixture: Fixture, normalized: dict[str, list[dict[str, Any]]], profile: Profile
) -> None:
    assert _content_attributes(normalized[fixture.id], profile) == []
    exported = json.dumps(GO.normalize(fixture.load()))
    assert [n for n in _NEEDLES if n in exported] == []
