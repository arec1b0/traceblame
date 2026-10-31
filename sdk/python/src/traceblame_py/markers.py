"""Correlation ids and markers set on spans (ADR-0006 section 5)."""

from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from traceblame_py._attributes import (
    BUNDLE_DIGEST,
    DECISION_ID,
    FAULT_INJECTION_ID,
    SESSION_ID,
    checked,
)

_SESSION: ContextVar[str | None] = ContextVar("traceblame_session", default=None)
_BUNDLE: ContextVar[str | None] = ContextVar("traceblame_bundle", default=None)
_DECISION: ContextVar[str | None] = ContextVar("traceblame_decision", default=None)


@contextmanager
def _scoped(var: ContextVar[str | None], name: str, value: str) -> Generator[None]:
    token = var.set(checked(name, value))
    try:
        yield
    finally:
        var.reset(token)


def session(session_id: str) -> AbstractContextManager[None]:
    """aegis.session.id on every span started inside."""
    return _scoped(_SESSION, SESSION_ID, session_id)


def bundle(digest: str) -> AbstractContextManager[None]:
    """aegis.bundle.digest on every span started inside; overrides the configured one."""
    return _scoped(_BUNDLE, BUNDLE_DIGEST, digest)


def decision(decision_id: str) -> AbstractContextManager[None]:
    """aegis.decision.id on every span started inside (spans that passed an AgentGate decision)."""
    return _scoped(_DECISION, DECISION_ID, decision_id)


def set_fault_injection_id(injection_id: str, span: trace.Span | None = None) -> None:
    """aegis.fault.injection.id on the span a fault is injected at (the current one by default)."""
    value = checked(FAULT_INJECTION_ID, injection_id)
    target = trace.get_current_span() if span is None else span
    if not target.is_recording():
        raise ValueError("set_fault_injection_id needs a recording span")
    target.set_attribute(FAULT_INJECTION_ID, value)


class ContextProcessor(SpanProcessor):
    """Stamps the ids of the current context on every span when it starts."""

    def __init__(self, bundle_digest: str | None) -> None:
        self._bundle = bundle_digest

    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        for name, value in (
            (SESSION_ID, _SESSION.get()),
            (BUNDLE_DIGEST, _BUNDLE.get() or self._bundle),
            (DECISION_ID, _DECISION.get()),
        ):
            if value is not None:
                span.set_attribute(name, value)

    def on_end(self, span: ReadableSpan) -> None:
        pass
