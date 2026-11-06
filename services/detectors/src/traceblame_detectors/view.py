"""The trace view: the only input a detector gets (#15).

A view is built from one `trace_summary` row and the trace's `spans` rows, as the contract
reads return them (spans_trace_view_read.sql, trace_summary_read.sql) or as a snapshot holds
them. The builder takes only the known columns, so anything else a source returns (resource
attributes, tenant, session, content references) never reaches a detector, and it keeps of
attrs only the keys it is given, after dropping every denied key (policy.py).

A detector sees a projection: the spans of the kinds it declared, and `attr()` answers only the
keys it declared. The projection's canonical form is what `input_digest` hashes.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from traceblame_detectors.model import Reads
from traceblame_detectors.policy import DECLARABLE, AttrType, is_denied

type AttrValue = str | int | bool | tuple[str, ...]


class UndeclaredReadError(LookupError):
    """A detector looked up an attribute it did not declare."""


class ViewError(ValueError):
    """A source returned rows that do not form a trace view."""


@dataclass(frozen=True, slots=True)
class SpanView:
    span_id: str
    parent_span_id: str
    service: str
    operation: str
    span_kind: str
    start_us: int
    duration_ms: float
    status_code: str
    provider: str
    model: str
    finish_reasons: tuple[str, ...]
    input_tokens: int
    output_tokens: int
    tool_name: str
    args_hash: str
    retrieval_scores: tuple[float, ...]
    retrieved_doc_ids: tuple[str, ...]
    bundle_digest: str
    error_type: str
    http_status_code: int
    source_sdk: str
    profile_version: str
    # Filtered attrs, raw string values as stored, sorted by key.
    attrs: tuple[tuple[str, str], ...]
    # The keys attr() answers; outside a projection, every key in attrs.
    declared: frozenset[str]

    def attr(self, key: str) -> AttrValue | None:
        """The typed value of a declared attribute, or None when the span does not carry it or
        its value does not parse as the profile type."""
        if key not in self.declared:
            raise UndeclaredReadError(key)
        raw = dict(self.attrs).get(key)
        if raw is None:
            return None
        return _parse(DECLARABLE[key], raw)

    def canonical(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "service": self.service,
            "operation": self.operation,
            "span_kind": self.span_kind,
            "start_us": self.start_us,
            "duration_ms": self.duration_ms,
            "status_code": self.status_code,
            "provider": self.provider,
            "model": self.model,
            "finish_reasons": list(self.finish_reasons),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "retrieval_scores": list(self.retrieval_scores),
            "retrieved_doc_ids": list(self.retrieved_doc_ids),
            "bundle_digest": self.bundle_digest,
            "error_type": self.error_type,
            "http_status_code": self.http_status_code,
            "source_sdk": self.source_sdk,
            "profile_version": self.profile_version,
            "attrs": dict(self.attrs),
        }


@dataclass(frozen=True, slots=True)
class TraceView:
    trace_id: str
    revision_ms: int
    assembler_version: str
    service: str
    root_span_id: str
    root_status_code: str
    start_us: int
    duration_ms: float
    span_count: int
    error_span_count: int
    bundle_digest: str
    # Sorted by (start_us, span_id): the order rows arrive in never matters.
    spans: tuple[SpanView, ...]

    def project(self, reads: Reads) -> "TraceView":
        """What a detector with these reads sees."""
        kinds = {k.value for k in reads.span_kinds}
        spans = tuple(
            replace(
                s,
                attrs=tuple((k, v) for k, v in s.attrs if k in reads.attrs),
                declared=reads.attrs,
            )
            for s in self.spans
            if s.span_kind in kinds
        )
        return replace(self, spans=spans)

    def canonical(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "revision_ms": self.revision_ms,
            "assembler_version": self.assembler_version,
            "service": self.service,
            "root_span_id": self.root_span_id,
            "root_status_code": self.root_status_code,
            "start_us": self.start_us,
            "duration_ms": self.duration_ms,
            "span_count": self.span_count,
            "error_span_count": self.error_span_count,
            "bundle_digest": self.bundle_digest,
            "spans": [s.canonical() for s in self.spans],
        }


SUMMARY_COLUMNS = (
    "trace_id",
    "assembler_version",
    "revision_ms",
    "service",
    "root_span_id",
    "root_status_code",
    "start_us",
    "duration_ms",
    "span_count",
    "error_span_count",
    "bundle_digest",
)

SPAN_COLUMNS = (
    "span_id",
    "parent_span_id",
    "service",
    "operation",
    "span_kind",
    "start_us",
    "duration_ms",
    "status_code",
    "provider",
    "model",
    "finish_reasons",
    "input_tokens",
    "output_tokens",
    "tool_name",
    "args_hash",
    "retrieval_scores",
    "retrieved_doc_ids",
    "bundle_digest",
    "error_type",
    "http_status_code",
    "source_sdk",
    "profile_version",
    "attrs",
)


def build_view(
    summary: Mapping[str, Any], spans: Iterable[Mapping[str, Any]], attr_keys: Iterable[str]
) -> TraceView:
    """A trace view from a summary row and span rows (column name -> value)."""
    keys = frozenset(k for k in attr_keys if not is_denied(k))
    trace_id = str(summary["trace_id"])
    built = sorted((_span(row, keys) for row in spans), key=lambda s: (s.start_us, s.span_id))
    if len({s.span_id for s in built}) != len(built):
        raise ViewError(f"trace {trace_id}: duplicate span ids")
    return TraceView(
        trace_id=trace_id,
        revision_ms=int(summary["revision_ms"]),
        assembler_version=str(summary["assembler_version"]),
        service=str(summary["service"]),
        root_span_id=str(summary["root_span_id"]),
        root_status_code=str(summary["root_status_code"]),
        start_us=int(summary["start_us"]),
        duration_ms=float(summary["duration_ms"]),
        span_count=int(summary["span_count"]),
        error_span_count=int(summary["error_span_count"]),
        bundle_digest=str(summary["bundle_digest"]),
        spans=tuple(built),
    )


def _span(row: Mapping[str, Any], keys: frozenset[str]) -> SpanView:
    raw_attrs: Mapping[str, Any] = row.get("attrs") or {}
    attrs = tuple(sorted((str(k), str(v)) for k, v in raw_attrs.items() if k in keys))
    return SpanView(
        span_id=str(row["span_id"]),
        parent_span_id=str(row["parent_span_id"]),
        service=str(row["service"]),
        operation=str(row["operation"]),
        span_kind=str(row["span_kind"]),
        start_us=int(row["start_us"]),
        duration_ms=float(row["duration_ms"]),
        status_code=str(row["status_code"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        finish_reasons=tuple(str(x) for x in row["finish_reasons"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        tool_name=str(row["tool_name"]),
        args_hash=str(row["args_hash"]),
        retrieval_scores=tuple(float(x) for x in row["retrieval_scores"]),
        retrieved_doc_ids=tuple(str(x) for x in row["retrieved_doc_ids"]),
        bundle_digest=str(row["bundle_digest"]),
        error_type=str(row["error_type"]),
        http_status_code=int(row["http_status_code"]),
        source_sdk=str(row["source_sdk"]),
        profile_version=str(row["profile_version"]),
        attrs=attrs,
        declared=frozenset(k for k, _ in attrs),
    )


def _parse(kind: AttrType, raw: str) -> AttrValue | None:
    """The exporter stores attribute values as strings: numbers in decimal, booleans as
    true/false, arrays as JSON (migration 0003)."""
    try:
        match kind:
            case AttrType.STRING:
                return raw
            case AttrType.INT:
                return int(raw)
            case AttrType.BOOL:
                return {"true": True, "false": False}.get(raw)
            case AttrType.STRING_LIST:
                value: Any = json.loads(raw)
                if isinstance(value, list) and all(isinstance(x, str) for x in value):  # pyright: ignore[reportUnknownVariableType]  # json.loads is untyped; checked here
                    return tuple(str(x) for x in value)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
                return None
    except ValueError:
        return None
