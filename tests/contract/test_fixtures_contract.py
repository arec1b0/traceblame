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
    # A scoped npm package (@scope/name) names its directory scope-name.
    assert fixture.path.parent.name in {
        f"{k.removeprefix('@').replace('/', '-')}-{v}" for k, v in provenance.packages.items()
    }


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


# The tracer scopes of the shims' helper spans: traceblame_py.spans.SCOPE and
# sdk/ts/src/spans.ts SCOPE.
SHIM_HELPER_SCOPES = {"traceblame_py", "traceblame_ts"}

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
        # ADR-0006 section 3: a shim's own helper spans (agent, tool) follow OpenInference
        # conventions, whatever SDK the fixture's LLM spans come from.
        helper = span["scope"]["name"] in SHIM_HELPER_SCOPES
        want = "openinference" if helper else fixture.provenance().sdk
        assert span["attributes"]["aegis.source.sdk"] == want
        assert re.fullmatch(r"\d+\.\d+\.\d+", span["attributes"]["aegis.normalizer.version"])


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_normalized_fixture_carries_no_content(
    fixture: Fixture, normalized: dict[str, list[dict[str, Any]]], profile: Profile
) -> None:
    assert _content_attributes(normalized[fixture.id], profile) == []
    exported = json.dumps(GO.normalize(fixture.load()))
    assert [n for n in _NEEDLES if n in exported] == []


def _output_features(profile: Profile) -> set[str]:
    return {a.name for a in profile.attributes if a.name.startswith("aegis.output.")}


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.id)
def test_output_features_only_on_llm_spans(
    fixture: Fixture, normalized: dict[str, list[dict[str, Any]]], profile: Profile
) -> None:
    # ADR-0006 section 4: the shims set output features only on spans the normalizer
    # classifies as llm, even when they come from another span's outcome (traceblame-ts).
    features = _output_features(profile)
    misplaced = [
        (span["span_id"], span["attributes"]["aegis.span.kind"])
        for span in normalized[fixture.id]
        if features & span["attributes"].keys() and span["attributes"]["aegis.span.kind"] != "llm"
    ]
    assert misplaced == []


def test_output_features_are_set_by_both_shims(
    normalized: dict[str, list[dict[str, Any]]], profile: Profile
) -> None:
    # Not vacuous: the application runs of traceblame-py (agentic_rag_*) and traceblame-ts
    # (runbook_*) carry them.
    features = _output_features(profile)
    for prefix in ("agentic_rag_", "runbook_"):
        ids = [f.id for f in FIXTURES if f.id.rsplit("/", 1)[-1].startswith(prefix)]
        assert ids, prefix
        assert any(features & span["attributes"].keys() for i in ids for span in normalized[i]), (
            prefix
        )
