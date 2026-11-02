"""The shims' export allowlists and aegis.* tables agree with the profile and the
normalizer's mapping tables (ADR-0006 sections 3, 5 and 6): traceblame-py's in Python,
traceblame-ts's in sdk/ts/src/contract.json."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceblame_contracts.profile import Profile
from traceblame_contracts.semconv import Snapshot
from traceblame_py import _allowlist, _attributes

ROOT = Path(__file__).resolve().parents[2]
MAPPINGS = ROOT / "collector" / "processor" / "normalizer" / "mappings"
OPENINFERENCE = MAPPINGS / "openinference" / "0.yaml"
OPENLLMETRY = MAPPINGS / "openllmetry" / "0.yaml"
TS_CONTRACT = ROOT / "sdk" / "ts" / "src" / "contract.json"

pytestmark = pytest.mark.contract

# Profile attributes only the normalizer sets; a shim must not export its own.
NORMALIZER_OWNED = {
    "aegis.profile.version",
    "aegis.span.kind",
    "aegis.source.sdk",
    "aegis.normalizer.version",
}
# The OTel SDK's default resource. The vendored snapshot holds the GenAI registry and what it
# references, not the telemetry registry.
SDK_RESOURCE = {"telemetry.sdk.language", "telemetry.sdk.name", "telemetry.sdk.version"}


def _load(path: Path) -> dict[str, Any]:
    table: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return table


def _openinference_keys() -> tuple[set[str], set[str]]:
    """Names and patterns the OpenInference table reads (kind.from, attr sources) or knows."""
    return _table_keys(OPENINFERENCE)


def _table_keys(path: Path) -> tuple[set[str], set[str]]:
    """Names and patterns a mapping table reads (kind.from, attr sources) or knows."""
    table = _load(path)
    names: set[str] = {str(table["kind"]["from"])} if "kind" in table else set()
    for row in table["map"]:
        names |= {src["attr"] for src in row["from"] if "attr" in src}
    patterns: set[str] = set()
    for entry in table.get("known", []):
        if isinstance(entry, str):
            names.add(entry)
        else:
            patterns.add(entry["pattern"])
    return names, patterns


def _content_rules(profile: Profile) -> tuple[set[str], list[re.Pattern[str]]]:
    names = {f.name for f in profile.forbidden}
    patterns: list[re.Pattern[str]] = []
    for path in sorted(MAPPINGS.rglob("*.yaml")):
        for entry in _load(path).get("content", []):
            if isinstance(entry, str):
                names.add(entry)
            else:
                patterns.append(re.compile(entry["pattern"]))
    return names, patterns


def _samples(pattern: str) -> list[str]:
    """Keys a pattern matches, for the few constructs the allowlist uses: \\d+, (a|b), \\."""
    body = pattern.removeprefix("^").removesuffix("$")
    assert re.fullmatch(r"(?:[a-z_]|\\\.|\\d\+|\([a-z_|]+\))+", body), f"unsupported: {pattern}"
    keys = [""]
    for part in re.findall(r"\\\.|\\d\+|\([a-z_|]+\)|[a-z_]", body):
        if part == r"\.":
            options = ["."]
        elif part == r"\d+":
            options = ["0", "17"]
        elif part.startswith("("):
            options = part[1:-1].split("|")
        else:
            options = [part]
        keys = [k + o for k in keys for o in options]
    return keys


def _exported_span_keys() -> list[str]:
    return [
        *_allowlist.SPAN_KEYS,
        *(k for p in _allowlist.SPAN_PATTERNS for k in _samples(p.pattern)),
    ]


def test_span_allowlist_is_profile_or_openinference_table(profile: Profile) -> None:
    span_attrs = {a.name for a in profile.scoped("span")}
    names, patterns = _openinference_keys()
    stray = sorted(k for k in _allowlist.SPAN_KEYS if k not in span_attrs and k not in names)
    assert stray == []
    assert {p.pattern for p in _allowlist.SPAN_PATTERNS} <= patterns
    assert not NORMALIZER_OWNED & _allowlist.SPAN_KEYS


def test_span_allowlist_is_not_content(profile: Profile) -> None:
    names, patterns = _content_rules(profile)
    content = [k for k in _exported_span_keys() if k in names or any(p.search(k) for p in patterns)]
    assert content == []
    assert all(_allowlist.keep_span_key(k) for k in _exported_span_keys())


def test_resource_allowlist(profile: Profile, snapshot: Snapshot) -> None:
    resource_attrs = {a.name for a in profile.scoped("resource")}
    stray = sorted(
        k
        for k in _allowlist.RESOURCE_KEYS
        if k not in resource_attrs
        and k not in SDK_RESOURCE
        and (k not in snapshot.attributes or snapshot.attributes[k].deprecated)
    )
    assert stray == []
    names, patterns = _content_rules(profile)
    assert not [
        k for k in _allowlist.RESOURCE_KEYS if k in names or any(p.search(k) for p in patterns)
    ]


def test_every_attribute_the_shim_sets_is_exported() -> None:
    for name, scope in _attributes.SCOPES.items():
        if scope == "span":
            assert _allowlist.keep_span_key(name), name
            assert not _allowlist.keep_resource_key(name), name
        else:
            assert _allowlist.keep_resource_key(name), name
            assert not _allowlist.keep_span_key(name), name


def test_shim_scopes_and_patterns_are_the_profiles(profile: Profile) -> None:
    for name, scope in _attributes.SCOPES.items():
        spec = profile.attribute(name)
        assert spec is not None, name
        assert spec.scope == scope, name
    for name, pattern in _attributes.PATTERNS.items():
        spec = profile.attribute(name)
        assert spec is not None, name
        assert spec.pattern == pattern, name
    patterned = {a.name for a in profile.attributes if a.pattern and a.name in _attributes.SCOPES}
    assert patterned == set(_attributes.PATTERNS)


# traceblame-ts (sdk/ts/src/contract.json, the shim's allowlist and aegis.* table).


def _ts() -> dict[str, Any]:
    contract: dict[str, Any] = json.loads(TS_CONTRACT.read_text(encoding="utf-8"))
    return contract


def test_ts_span_allowlist_is_profile_or_its_tables(profile: Profile) -> None:
    # Helper spans follow OpenInference conventions; the AI SDK's spans reach the normalizer
    # through OpenLLMetry-js (ADR-0006 sections 3 and 6).
    span_attrs = {a.name for a in profile.scoped("span")}
    names: set[str] = set()
    for table in (OPENINFERENCE, OPENLLMETRY):
        names |= _table_keys(table)[0]
    keys = set(_ts()["span_keys"])
    assert sorted(k for k in keys if k not in span_attrs and k not in names) == []
    assert not NORMALIZER_OWNED & keys


def test_ts_span_allowlist_is_not_content(profile: Profile) -> None:
    names, patterns = _content_rules(profile)
    keys = [*_ts()["span_keys"], *_ts()["exception_keys"]]
    assert [k for k in keys if k in names or any(p.search(k) for p in patterns)] == []
    assert _ts()["exception_keys"] == ["exception.type"]


def test_ts_resource_allowlist(profile: Profile, snapshot: Snapshot) -> None:
    resource_attrs = {a.name for a in profile.scoped("resource")}
    keys = _ts()["resource_keys"]
    stray = sorted(
        k
        for k in keys
        if k not in resource_attrs
        and k not in SDK_RESOURCE
        and (k not in snapshot.attributes or snapshot.attributes[k].deprecated)
    )
    assert stray == []
    names, patterns = _content_rules(profile)
    assert not [k for k in keys if k in names or any(p.search(k) for p in patterns)]


def test_ts_shim_scopes_and_patterns_are_the_profiles(profile: Profile) -> None:
    contract = _ts()
    for name, scope in contract["scopes"].items():
        spec = profile.attribute(name)
        assert spec is not None, name
        assert spec.scope == scope, name
        exported = contract["span_keys"] if scope == "span" else contract["resource_keys"]
        assert name in exported, name
    for name, pattern in contract["patterns"].items():
        spec = profile.attribute(name)
        assert spec is not None, name
        assert spec.pattern == pattern, name
    patterned = {a.name for a in profile.attributes if a.pattern and a.name in contract["scopes"]}
    assert patterned == set(contract["patterns"])
