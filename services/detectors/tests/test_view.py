"""The trace view: what a detector can see, and what it never sees (rule 9)."""

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

from traceblame_detectors.model import Reads, SpanKind
from traceblame_detectors.policy import DECLARABLE
from traceblame_detectors.testing import (
    INJECTED_TEXT,
    INJECTION_ID,
    RUN_ID,
    span_rows,
    summary_row,
)
from traceblame_detectors.view import UndeclaredReadError, ViewError, build_view

MARKER_KEYS = ("aegis.fault.injection.id", "aegis.eval.run.id", "exception.message")


def strings(value: Any) -> Iterator[str]:
    """Every string anywhere in a view: field values, attr keys and values, nested tuples."""
    if isinstance(value, str):
        yield value
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield f.name
            yield from strings(getattr(value, f.name))
    elif isinstance(value, tuple | list | frozenset | set):
        for v in value:  # pyright: ignore[reportUnknownVariableType]
            yield from strings(v)


def test_markers_never_reach_the_view_even_when_requested() -> None:
    view = build_view(summary_row(), span_rows(), [*DECLARABLE, *MARKER_KEYS])
    found = list(strings(view))
    assert len(found) > 100  # the scan saw the whole view
    for needle in (
        *MARKER_KEYS,
        RUN_ID,
        INJECTION_ID,
        INJECTED_TEXT,
        "tenant-eval",
        "resource_attrs",
    ):
        assert not any(needle in s for s in found), needle


def test_the_marker_scan_can_fail() -> None:
    # Sabotage: a builder that kept every attr would leak the injection id.
    rows = span_rows()
    view = build_view(summary_row(), rows, [])
    leaky = dataclasses.replace(
        view, spans=(dataclasses.replace(view.spans[2], attrs=(("x", INJECTION_ID),)),)
    )
    assert any(INJECTION_ID in s for s in strings(leaky))


def test_row_order_never_matters() -> None:
    rows = span_rows()
    shuffled = rows[1:] + rows[:1]
    a = build_view(summary_row(), rows, DECLARABLE)
    b = build_view(summary_row(), list(reversed(shuffled)), DECLARABLE)
    assert a == b
    assert [s.start_us for s in a.spans] == sorted(s.start_us for s in a.spans)


def test_duplicate_span_ids_are_not_a_trace() -> None:
    rows = span_rows()
    with pytest.raises(ViewError):
        build_view(summary_row(), [*rows, rows[1]], [])


def test_a_projection_keeps_declared_kinds_and_attributes_only() -> None:
    view = build_view(summary_row(), span_rows(), DECLARABLE)
    llm = view.project(Reads(frozenset({SpanKind.LLM}), frozenset({"aegis.output.parse_ok"})))
    assert [s.span_kind for s in llm.spans] == ["llm"]
    span = llm.spans[0]
    assert span.attr("aegis.output.parse_ok") is False
    with pytest.raises(UndeclaredReadError):
        span.attr("aegis.output.cited_doc_ids")
    assert dict(span.attrs) == {"aegis.output.parse_ok": "false"}


@pytest.mark.parametrize(
    ("key", "raw", "value"),
    [
        ("aegis.output.parse_ok", "true", True),
        ("aegis.output.parse_ok", "1", None),
        ("aegis.output.cited_doc_ids", '["a","b"]', ("a", "b")),
        ("aegis.output.cited_doc_ids", "[1]", None),
        ("aegis.output.cited_doc_ids", "not json", None),
        ("gen_ai.request.max_tokens", "32", 32),
        ("gen_ai.request.max_tokens", "x", None),
        ("gen_ai.response.model", "qwen2.5:7b", "qwen2.5:7b"),
    ],
)
def test_attributes_parse_as_their_profile_type(key: str, raw: str, value: object) -> None:
    rows = span_rows()
    rows[3]["attrs"] = {key: raw}
    view = build_view(summary_row(), rows, [key]).project(
        Reads(frozenset({SpanKind.LLM}), frozenset({key}))
    )
    assert view.spans[0].attr(key) == value


def test_a_missing_attribute_is_none() -> None:
    view = build_view(summary_row(), span_rows(), DECLARABLE).project(
        Reads(frozenset({SpanKind.TOOL}), frozenset({"gen_ai.tool.call.id"}))
    )
    assert view.spans[0].attr("gen_ai.tool.call.id") is None


def test_the_canonical_form_covers_every_field() -> None:
    view = build_view(summary_row(), span_rows(), DECLARABLE)
    trace = view.canonical()
    assert set(trace) == {f.name for f in dataclasses.fields(view)}
    span = view.spans[0]
    assert set(span.canonical()) == {f.name for f in dataclasses.fields(span)} - {"declared"}
