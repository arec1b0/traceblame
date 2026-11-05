"""tools/faults/agentic_rag_app.py Injection: a fault counts as applied only once its marker is on
the span (PR #64, Bugbot). If marking fails, nothing is recorded as applied, and the error stops
the fault before its effect reaches the app. Spans come from an in-memory SDK provider; no app,
no eval data."""

import os
import tempfile
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# agentic_rag_app puts the app checkout on sys.path at import; an empty directory stands in.
os.environ.setdefault("CAPTURE_APP_DIR", tempfile.mkdtemp(prefix="no-app-"))

import agentic_rag_app  # tools/faults, imported after the environment above

from traceblame_faults import ids, ledger, plan

RUN = "faults-20261106-s7-abc1234"
REF = "Contract A__Governing Law"
DIGEST = "sha256:" + "a" * 64
MARKER = "aegis.fault.injection.id"


@pytest.fixture
def injection(monkeypatch: pytest.MonkeyPatch) -> agentic_rag_app.Injection:
    monkeypatch.setenv("TRACEBLAME_EVAL_RUN_ID", RUN)
    entry = plan.Entry(
        query_ref=REF,
        app="agentic-rag",
        kind="tool_failure",
        scenario="tool_failure-agentic-rag",
        params={"mode": "backend_error", "tool": "retriever", "call_index": 1},
        injection_id=ids.injection_id(RUN, "tool_failure", REF),
        seed=7,
    )
    query = plan.Query(REF, "agentic-rag", "cuad", "answerable", "none", ())
    inj = agentic_rag_app.Injection(entry, query, DIGEST)
    inj.trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    return inj


@pytest.fixture
def spans() -> Iterator[tuple[trace.Tracer, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("test"), exporter
    provider.shutdown()


def _attrs(span: ReadableSpan) -> dict[str, object]:
    return dict(span.attributes or {})


def test_a_failed_marker_records_nothing_as_applied(injection: agentic_rag_app.Injection) -> None:
    unrecorded = trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT)
    with pytest.raises(ValueError, match="recording span"):
        injection.fire(unrecorded, {"removed_n": 2})
    assert injection.applied is None
    assert injection.injected_at is None
    assert injection.marked == {}
    rec = injection.record()
    assert ledger.status(rec) == "not_reached"
    assert rec.injection_id == ""


def test_a_marked_span_makes_the_fault_applied(
    injection: agentic_rag_app.Injection, spans: tuple[trace.Tracer, InMemorySpanExporter]
) -> None:
    tracer, exporter = spans
    with tracer.start_as_current_span("tool") as span:
        injection.fire(span, {"removed_n": 2})
    (done,) = exporter.get_finished_spans()
    assert _attrs(done)[MARKER] == injection.entry.injection_id
    assert injection.applied == {"removed_n": 2}
    rec = injection.record()
    assert ledger.status(rec) == "applied"
    assert rec.params["removed_n"] == 2


def test_a_later_failed_marker_adds_nothing_to_an_applied_fault(
    injection: agentic_rag_app.Injection, spans: tuple[trace.Tracer, InMemorySpanExporter]
) -> None:
    tracer, _ = spans
    with tracer.start_as_current_span("retriever") as span:
        injection.fire(span, {"removed_n": 1, "retrievals": 1})
    with pytest.raises(ValueError, match="recording span"):
        injection.fire(
            trace.NonRecordingSpan(trace.INVALID_SPAN_CONTEXT), {"removed_n": 5, "retrievals": 1}
        )
    assert injection.applied == {"removed_n": 1, "retrievals": 1}
    assert len(injection.marked) == 1
