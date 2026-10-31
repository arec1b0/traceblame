"""Gap report: what each SDK export lacks against the profile, before and after the normalizer.

It does not gate. `make contract` writes it to .contract/ and CI uploads it as an artifact:

- raw violations: the schema violations of the export as the SDK wrote it (identity);
- missing: recommended and conditionally required attributes that are still absent after
  the Go normalizer, per span kind.
"""

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from traceblame_contracts.normalizers import Normalizer
from traceblame_contracts.otlp import flatten_otlp_json
from traceblame_contracts.profile import SPAN_KIND_ATTRIBUTE, Profile
from traceblame_contracts.validate import validate_span

_GAP_LEVELS = ("recommended", "conditionally_required")


# Conditions the report can evaluate; other conditionally required attributes count as
# missing whenever they are absent, with their condition shown.
def _on_error(span: Mapping[str, Any]) -> bool:
    return span["status"]["code"] == "STATUS_CODE_ERROR"


_CONDITIONS: dict[str, Callable[[Mapping[str, Any]], bool]] = {
    "error.type": _on_error,
    # Read from the client's error message (#29): successful calls never carry it.
    "http.response.status_code": _on_error,
}


@dataclass(frozen=True)
class FixtureGaps:
    fixture: str
    raw_violations: dict[str, int]
    missing: dict[str, int]


def missing_attributes(span: Mapping[str, Any], profile: Profile) -> list[str]:
    """Recommended and conditionally required attributes the span's kind wants but lacks."""
    kind = span["attributes"].get(SPAN_KIND_ATTRIBUTE)
    if not isinstance(kind, str):
        return []
    out: list[str] = []
    for attr in profile.attributes:
        level = attr.level_for(kind)
        if level not in _GAP_LEVELS:
            continue
        condition = _CONDITIONS.get(attr.name)
        if condition is not None and not condition(span):
            continue
        holder = span["resource"] if attr.scope == "resource" else span["attributes"]
        if attr.name in holder:
            continue
        suffix = f"{level}: {attr.condition}" if attr.condition else level
        out.append(f"{attr.name} ({suffix})")
    return out


def fixture_gaps(
    fixture_id: str,
    otlp: Mapping[str, Any],
    profile: Profile,
    schema: Mapping[str, Any],
    raw: Normalizer,
    normalized: Normalizer,
) -> FixtureGaps:
    violations: Counter[str] = Counter(
        f"{v.path}: {v.message}"
        for span in flatten_otlp_json(raw.normalize(otlp))
        for v in validate_span(span, schema)
    )
    missing: Counter[str] = Counter(
        f"{span['attributes'][SPAN_KIND_ATTRIBUTE]}: {gap}"
        for span in flatten_otlp_json(normalized.normalize(otlp))
        for gap in missing_attributes(span, profile)
    )
    return FixtureGaps(fixture_id, dict(sorted(violations.items())), dict(sorted(missing.items())))


def render_json(gaps: Sequence[FixtureGaps], profile_id: str, revision: str) -> str:
    doc = {"profile": profile_id, "revision": revision, "fixtures": [asdict(g) for g in gaps]}
    return json.dumps(doc, indent=2) + "\n"


def render_markdown(gaps: Sequence[FixtureGaps], profile_id: str, revision: str) -> str:
    lines = [
        f"# Contract gap report: {profile_id} (revision {revision})",
        "",
        "Raw: schema violations of the SDK export as captured. Missing: recommended and",
        "conditionally required attributes still absent after the normalizer.",
        "",
        "| Fixture | Raw violations | Missing after normalizer |",
        "|---|---:|---:|",
    ]
    lines += [
        f"| `{g.fixture}` | {sum(g.raw_violations.values())} | {sum(g.missing.values())} |"
        for g in gaps
    ]
    for g in gaps:
        if not (g.raw_violations or g.missing):
            continue
        lines += ["", f"## `{g.fixture}`", ""]
        lines += [f"- raw: {msg} (n={n})" for msg, n in g.raw_violations.items()]
        lines += [f"- missing: {msg} (n={n})" for msg, n in g.missing.items()]
    return "\n".join(lines) + "\n"
