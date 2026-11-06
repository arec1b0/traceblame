"""The detectors' contracts (#15): what a detector may read matches the profile and the spans
view; the embedded SQL equals the canonical statements; version locks and vectors only grow."""

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from traceblame_contracts.profile import load_profile
from traceblame_detectors.locks import FRAMEWORK_LOCK, PLUGINS_LOCK, append_only_problems, load_lock
from traceblame_detectors.policy import COLUMN_ATTRS, DECLARABLE, DENIED_ATTRS, is_denied
from traceblame_detectors.view import SPAN_COLUMNS, SUMMARY_COLUMNS
from traceblame_storage.migrations import git_text

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "schemas" / "profile" / "aegis-telemetry.v1.yaml"
DETECTORS = ROOT / "services" / "detectors"
EMBEDDED = DETECTORS / "src" / "traceblame_detectors" / "sql"
INTEGRATION = ROOT / "tests" / "integration"
CONTRACT = ROOT / "packages" / "storage" / "src" / "traceblame_storage" / "contract"
MIGRATIONS = ROOT / "packages" / "storage" / "src" / "traceblame_storage" / "migrations"
SPANS_VIEW = MIGRATIONS / "clickhouse" / "0003_expand_spans_from_otel.sql"

pytestmark = pytest.mark.contract


def _span_attrs() -> dict[str, str]:
    profile = load_profile(PROFILE)
    return {a.name: a.type for a in profile.attributes if a.scope == "span"}


def test_declarable_columns_and_denied_cover_the_profiles_span_attributes() -> None:
    span = _span_attrs()
    assert len(span) > 20
    denied = {k for k in span if is_denied(k)}
    assert set(DECLARABLE) | COLUMN_ATTRS | denied == set(span)
    assert not set(DECLARABLE) & (COLUMN_ATTRS | denied)
    for key, kind in DECLARABLE.items():
        assert kind.value == span[key], key


def test_markers_and_forbidden_attributes_are_denied() -> None:
    profile = load_profile(PROFILE)
    forbidden = {f.name for f in profile.forbidden}
    assert forbidden
    assert forbidden <= DENIED_ATTRS
    markers = {
        a.name for a in profile.attributes if a.name.startswith(("aegis.eval.", "aegis.fault."))
    }
    assert markers == {"aegis.eval.run.id", "aegis.fault.injection.id"}
    assert all(is_denied(m) for m in markers)


def test_column_attributes_are_what_the_spans_view_maps_into_columns() -> None:
    sql = SPANS_VIEW.read_text(encoding="utf-8")
    block = sql[
        sql.index("mapFilter((k, v) -> k NOT IN (") : sql.index("), SpanAttributes) AS attrs")
    ]
    mapped = set(re.findall(r"'([^']+)'", block))
    assert mapped == COLUMN_ATTRS


def _select_aliases(sql: str) -> list[str]:
    select = sql[sql.index("SELECT") : sql.index("\nFROM")]
    return [re.split(r"\s+AS\s+|\.", item.strip())[-1].strip() for item in _top_level(select[6:])]


def _top_level(text: str) -> list[str]:
    out: list[str] = []
    depth, cur = 0, ""
    for c in text:
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
    return [*out, cur]


@pytest.mark.parametrize(
    ("name", "columns"),
    [("trace_summary_read.sql", SUMMARY_COLUMNS), ("spans_trace_view_read.sql", SPAN_COLUMNS)],
)
def test_the_view_columns_are_the_contract_reads_columns(
    name: str, columns: tuple[str, ...]
) -> None:
    assert tuple(_select_aliases((CONTRACT / name).read_text(encoding="utf-8"))) == columns


def test_the_spans_read_never_selects_resource_attributes_or_correlation_ids() -> None:
    sql = (CONTRACT / "spans_trace_view_read.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in sql.splitlines() if not line.startswith("--"))
    for column in ("resource_attrs", "tenant_id", "session_id", "decision_id", "content_ref"):
        assert column not in body, column


def test_the_embedded_statements_are_the_canonical_ones() -> None:
    names = sorted(p.name for p in EMBEDDED.glob("*.sql"))
    assert names == sorted(p.name for p in INTEGRATION.glob("detectors_*.sql"))
    assert len(names) == 6
    for name in names:
        assert (EMBEDDED / name).read_bytes() == (INTEGRATION / name).read_bytes(), name


def _base_json(path: Path) -> dict[str, Any] | None:
    ref = os.environ.get("CONTRACT_BASE_REF", "main")
    text = git_text(["show", f"{ref}:{path.relative_to(ROOT).as_posix()}"], ROOT)
    return None if text is None else json.loads(text)


@pytest.mark.parametrize("name", [FRAMEWORK_LOCK, PLUGINS_LOCK])
def test_version_locks_only_grow_against_the_base_revision(name: str) -> None:
    path = DETECTORS / "src" / "traceblame_detectors" / name
    base = _base_json(path)
    if base is None:
        pytest.skip(f"{name} is not on the base revision yet")
    assert append_only_problems(base, load_lock(name)) == []


def test_released_vectors_never_change() -> None:
    vectors = sorted((DETECTORS / "vectors").glob("v*.json"))
    assert vectors
    for path in vectors:
        base = _base_json(path)
        if base is not None:
            assert json.loads(path.read_text(encoding="utf-8")) == base, path.name
