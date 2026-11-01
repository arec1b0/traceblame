"""Agent, retriever and tool spans in OpenInference conventions, for code that no
instrumentation covers. They carry ids, scores and hashes, never text (ADR-0006 section 2)."""

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from importlib.metadata import version
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Status, StatusCode

from traceblame_py import _provider
from traceblame_py._attributes import ARGS_HASH, DOC_IDS, SCORES
from traceblame_py.args_hash import args_hash

SCOPE = "traceblame_py"
_KIND = "openinference.span.kind"


def _tracer() -> trace.Tracer:
    return trace.get_tracer(SCOPE, version("traceblame-py"))


@contextmanager
def agent_span(name: str) -> Generator[trace.Span]:
    """An AGENT span named after the agent, current inside the block."""
    with _tracer().start_as_current_span(
        name, attributes={_KIND: "AGENT", "agent.name": name}
    ) as span:
        yield span


class Tool:
    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def fail(self, error_type: str) -> None:
        """The tool reported a failure without raising. error_type is a class, not a message."""
        self._span.set_status(Status(StatusCode.ERROR))
        self._span.set_attribute("error.type", error_type)


@contextmanager
def tool_span(
    name: str, arguments: str | Mapping[str, Any] | Sequence[Any] | None = None
) -> Generator[Tool]:
    """A TOOL span with aegis.tool.args_hash of the arguments; the arguments are not recorded.
    Arguments that are not JSON-serializable get no hash."""
    attributes: dict[str, str] = {_KIND: "TOOL", "tool.name": name}
    if arguments is not None:
        with suppress(TypeError, ValueError):
            attributes[ARGS_HASH] = args_hash(arguments)
    with _tracer().start_as_current_span(name, attributes=attributes) as span:
        yield Tool(span)


class Retrieval:
    def __init__(self, span: trace.Span) -> None:
        self._span = span
        self.ids: tuple[str, ...] | None = None

    def set_documents(self, ids: Sequence[str], scores: Sequence[float] | None = None) -> None:
        """Ids of the retrieved documents in rank order, and their scores aligned with them."""
        if scores is not None and len(scores) != len(ids):
            raise ValueError("scores must be aligned with ids")
        self.ids = tuple(ids)
        for i, doc_id in enumerate(self.ids):
            self._span.set_attribute(f"retrieval.documents.{i}.document.id", doc_id)
        self._span.set_attribute(DOC_IDS, self.ids)
        if scores is not None:
            values = tuple(float(s) for s in scores)
            for i, score in enumerate(values):
                self._span.set_attribute(f"retrieval.documents.{i}.document.score", score)
            self._span.set_attribute(SCORES, values)


@contextmanager
def retriever_span(source: str) -> Generator[Retrieval]:
    """A RETRIEVER span over the data source `source`. The ids set on it become candidates for
    aegis.output.cited_doc_ids of later LLM calls in the same trace."""
    attributes = {_KIND: "RETRIEVER", "gen_ai.data_source.id": source}
    with _tracer().start_as_current_span(source, attributes=attributes) as span:
        retrieval = Retrieval(span)
        yield retrieval
    shim = _provider.active()
    if (
        shim is not None
        and retrieval.ids is not None
        and isinstance(span, ReadableSpan)
        and span.context is not None
        and span.end_time is not None
    ):
        shim.registry.add(span.context.trace_id, span.end_time, retrieval.ids)
