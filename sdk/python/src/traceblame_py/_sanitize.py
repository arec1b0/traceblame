"""The exporter wrapper that computes features and rebuilds spans from the allowlist."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status, StatusCode

from traceblame_py._allowlist import (
    EXCEPTION_EVENT,
    EXCEPTION_KEYS,
    keep_resource_key,
    keep_span_key,
    status_prefix,
)
from traceblame_py._attributes import CITED_DOC_IDS, PARSE_OK
from traceblame_py.features import RetrievalRegistry, cited_doc_ids, parse_ok

_KIND = "openinference.span.kind"
_INVOCATION_PARAMETERS = "llm.invocation_parameters"
_OUTPUT = "llm.output_messages.0.message.content"
_REQUEST_MODEL = "gen_ai.request.model"
_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"


class SanitizingExporter(SpanExporter):
    """Hands the inner exporter only sanitized copies: ended spans are immutable, so features
    computed after a call cannot be set on the span itself."""

    def __init__(self, inner: SpanExporter, registry: RetrievalRegistry) -> None:
        self._inner = inner
        self._registry = registry

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([sanitize(s, self._registry) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def sanitize(span: ReadableSpan, registry: RetrievalRegistry) -> ReadableSpan:
    attrs: Mapping[str, Any] = span.attributes or {}
    kept = {k: v for k, v in attrs.items() if keep_span_key(k)}
    if attrs.get(_KIND) == "LLM":
        for key, value in _llm_features(span, attrs, registry).items():
            kept.setdefault(key, value)
    error = span.status.status_code is StatusCode.ERROR
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=Resource(
            {k: v for k, v in span.resource.attributes.items() if keep_resource_key(k)},
            span.resource.schema_url,
        ),
        attributes=kept,
        events=[
            Event(
                EXCEPTION_EVENT,
                {k: v for k, v in (e.attributes or {}).items() if k in EXCEPTION_KEYS},
                e.timestamp,
            )
            for e in span.events
            if e.name == EXCEPTION_EVENT
        ],
        links=[
            Link(
                link.context, {k: v for k, v in (link.attributes or {}).items() if keep_span_key(k)}
            )
            for link in span.links
        ],
        kind=span.kind,
        status=Status(
            span.status.status_code, status_prefix(span.status.description) if error else None
        ),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def _llm_features(
    span: ReadableSpan, attrs: Mapping[str, Any], registry: RetrievalRegistry
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    raw = attrs.get(_INVOCATION_PARAMETERS)
    params = _json_object(raw)
    model = params.get("model")
    if isinstance(model, str) and model:
        out[_REQUEST_MODEL] = model
    max_tokens = params.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        out[_REQUEST_MAX_TOKENS] = max_tokens
    if span.status.status_code is StatusCode.ERROR:
        return out
    output = attrs.get(_OUTPUT)
    text = output if isinstance(output, str) else None
    ok = parse_ok(raw if isinstance(raw, str) else None, text)
    if ok is not None:
        out[PARSE_OK] = ok
    if span.context is not None and span.start_time is not None:
        candidates = registry.before(span.context.trace_id, span.start_time)
        if candidates is not None:
            out[CITED_DOC_IDS] = tuple(cited_doc_ids(candidates, text))
    return out


def _json_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]
