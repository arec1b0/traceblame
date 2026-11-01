"""Helper spans in OpenInference conventions carry ids, scores and hashes, never text."""

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceblame_py import Shim, agent_span, args_hash, retriever_span, tool_span

from .conftest import by_name, dump, exported, reset_global_provider

CANARY = "CANARY-91be"


def test_agent_span(shim: Shim, exporter: InMemorySpanExporter) -> None:
    with agent_span("RAGAgent"):
        pass
    (s,) = exported(shim, exporter)
    assert s.name == "RAGAgent"
    assert dict(s.attributes or {}) == {
        "openinference.span.kind": "AGENT",
        "agent.name": "RAGAgent",
    }


def test_tool_span_hashes_arguments_and_drops_them(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    arguments = {"query": CANARY, "top_k": 5}
    with agent_span("a"), tool_span("retriever", arguments):
        pass
    spans = exported(shim, exporter)
    tool = by_name(spans, "retriever")
    assert dict(tool.attributes or {}) == {
        "openinference.span.kind": "TOOL",
        "tool.name": "retriever",
        "aegis.tool.args_hash": args_hash(json.dumps(arguments)),
    }
    assert tool.parent is not None
    assert CANARY not in dump(spans)


def test_tool_span_accepts_argument_text(shim: Shim, exporter: InMemorySpanExporter) -> None:
    with tool_span("calc", '{"expression": "1+1"}'):
        pass
    (s,) = exported(shim, exporter)
    assert (s.attributes or {})["aegis.tool.args_hash"] == args_hash('{"expression":"1+1"}')


def test_tool_span_without_hashable_arguments_still_runs(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    with tool_span("t", {"when": object()}):
        pass
    with tool_span("u"):
        pass
    spans = exported(shim, exporter)
    assert all("aegis.tool.args_hash" not in (s.attributes or {}) for s in spans)


def test_tool_failure(shim: Shim, exporter: InMemorySpanExporter) -> None:
    with tool_span("calc", {"expression": CANARY}) as tool:
        tool.fail("ToolFailure")
    (s,) = exported(shim, exporter)
    assert s.status.status_code is StatusCode.ERROR
    assert s.status.description is None
    assert (s.attributes or {})["error.type"] == "ToolFailure"


def test_an_exception_keeps_only_its_type(shim: Shim, exporter: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), tool_span("calc", {}):
        raise RuntimeError(CANARY)
    spans = exported(shim, exporter)
    assert CANARY not in dump(spans)
    (s,) = spans
    assert s.status.status_code is StatusCode.ERROR
    assert [(e.name, dict(e.attributes or {})) for e in s.events] == [
        ("exception", {"exception.type": "RuntimeError"})
    ]


def test_retriever_span_records_ids_and_scores_only(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    with retriever_span("chroma") as r:
        r.set_documents(["d-1", "d-2"], [0.81, 0.4])
    (s,) = exported(shim, exporter)
    assert s.name == "chroma"
    attrs = dict(s.attributes or {})
    assert attrs == {
        "openinference.span.kind": "RETRIEVER",
        "gen_ai.data_source.id": "chroma",
        "retrieval.documents.0.document.id": "d-1",
        "retrieval.documents.0.document.score": 0.81,
        "retrieval.documents.1.document.id": "d-2",
        "retrieval.documents.1.document.score": 0.4,
        "aegis.retrieval.doc_ids": ("d-1", "d-2"),
        "aegis.retrieval.scores": (0.81, 0.4),
    }


def test_retriever_span_without_scores(shim: Shim, exporter: InMemorySpanExporter) -> None:
    with retriever_span("chroma") as r:
        r.set_documents(["d-1"])
    (s,) = exported(shim, exporter)
    attrs = dict(s.attributes or {})
    assert attrs["aegis.retrieval.doc_ids"] == ("d-1",)
    assert "aegis.retrieval.scores" not in attrs
    assert "retrieval.documents.0.document.score" not in attrs


def test_retriever_span_rejects_misaligned_scores(shim: Shim) -> None:
    with retriever_span("chroma") as r, pytest.raises(ValueError, match="aligned"):
        r.set_documents(["d-1", "d-2"], [0.5])


def test_retriever_span_with_nothing_found(shim: Shim, exporter: InMemorySpanExporter) -> None:
    with retriever_span("chroma") as r:
        r.set_documents([], [])
    (s,) = exported(shim, exporter)
    assert (s.attributes or {})["aegis.retrieval.doc_ids"] == ()


def test_helpers_without_a_shim_are_harmless() -> None:
    reset_global_provider()
    with agent_span("a"), tool_span("t", {"x": 1}) as tool, retriever_span("r") as r:
        tool.fail("E")
        r.set_documents(["d"], [1.0])
    assert not trace.get_current_span().get_span_context().is_valid
