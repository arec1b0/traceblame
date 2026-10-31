"""The positive export allowlist (ADR-0006 section 3): what is not listed never leaves."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode

from traceblame_py import Shim, ShimConfig, instrument

from .conftest import dump, exported, reset_global_provider

CANARY = "CANARY-7f3a"


def test_only_allowlisted_keys_events_and_link_attributes_are_exported(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("linked") as other:
        pass
    attrs = {
        "openinference.span.kind": "LLM",
        "llm.model_name": "qwen2.5:7b",
        "llm.token_count.prompt": 3,
        "retrieval.documents.0.document.id": "d-1",
        "retrieval.documents.0.document.score": 0.5,
        "retrieval.documents.0.document.content": CANARY,
        "input.value": CANARY,
        "output.value": CANARY,
        "llm.invocation_parameters": CANARY,
        "llm.input_messages.0.message.content": CANARY,
        "gen_ai.input.messages": CANARY,
        "some.vendor.key": CANARY,
        "aegis.span.kind": "llm",
    }
    link = Link(other.get_span_context(), {"aegis.session.id": "s", "note": CANARY})
    with tracer.start_as_current_span("s", attributes=attrs, links=[link]) as span:
        span.add_event("custom", {"x": CANARY})
        span.add_event(
            "exception",
            {
                "exception.type": "ValueError",
                "exception.message": CANARY,
                "exception.stacktrace": CANARY,
            },
        )
        span.set_status(
            Status(StatusCode.ERROR, f"openai.BadRequestError: Error code: 400 - {CANARY}")
        )
    spans = exported(shim, exporter)
    assert CANARY not in dump(spans)
    s = next(x for x in spans if x.name == "s")
    assert dict(s.attributes or {}) == {
        "openinference.span.kind": "LLM",
        "llm.model_name": "qwen2.5:7b",
        "llm.token_count.prompt": 3,
        "retrieval.documents.0.document.id": "d-1",
        "retrieval.documents.0.document.score": 0.5,
    }
    assert [(e.name, dict(e.attributes or {})) for e in s.events] == [
        ("exception", {"exception.type": "ValueError"})
    ]
    assert [dict(link.attributes or {}) for link in s.links] == [{"aegis.session.id": "s"}]
    assert s.links[0].context.span_id == other.get_span_context().span_id
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description == "openai.BadRequestError: Error code: 400"


@pytest.mark.parametrize(
    ("description", "kept"),
    [
        ("Error code: 429 - slow down", "Error code: 429"),
        ("RateLimitError: Error code: 429", "RateLimitError: Error code: 429"),
        (f"ValueError: {CANARY}", None),
        (f"Error code: 4290 {CANARY}", None),
    ],
)
def test_status_keeps_only_the_error_code_prefix(
    shim: Shim, exporter: InMemorySpanExporter, description: str, kept: str | None
) -> None:
    with trace.get_tracer("t").start_as_current_span("s") as span:
        span.set_status(Status(StatusCode.ERROR, description))
    (s,) = exported(shim, exporter)
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description == kept


def test_resource_keeps_only_allowlisted_keys(
    monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter
) -> None:
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", f"secret={CANARY},deployment.environment=dev")
    reset_global_provider()
    s = instrument(
        service_name="svc",
        service_version="1.2.3",
        config=ShimConfig(tenant_id="t"),
        exporter=exporter,
    )
    try:
        with trace.get_tracer("t").start_as_current_span("x"):
            pass
        (span,) = exported(s, exporter)
    finally:
        s.shutdown()
        reset_global_provider()
    res = dict(span.resource.attributes)
    assert CANARY not in dump([span])
    assert set(res) == {
        "service.name",
        "service.version",
        "service.instance.id",
        "aegis.tenant.id",
        "telemetry.sdk.language",
        "telemetry.sdk.name",
        "telemetry.sdk.version",
    }
    assert res["service.version"] == "1.2.3"


def test_span_identity_and_timing_survive(shim: Shim, exporter: InMemorySpanExporter) -> None:
    tracer = trace.get_tracer("scope-x", "9.9")
    with (
        tracer.start_as_current_span("parent") as parent,
        tracer.start_as_current_span("child") as child,
    ):
        pass
    spans = {s.name: s for s in exported(shim, exporter)}
    c = spans["child"]
    assert c.context is not None
    assert c.context.span_id == child.get_span_context().span_id
    assert c.parent is not None
    assert c.parent.span_id == parent.get_span_context().span_id
    assert c.instrumentation_scope is not None
    assert (c.instrumentation_scope.name, c.instrumentation_scope.version) == ("scope-x", "9.9")
    assert c.start_time is not None
    assert c.end_time is not None
    assert c.end_time >= c.start_time
