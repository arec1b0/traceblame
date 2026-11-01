"""instrument(): the shim's sealed tracer provider and its single export path (ADR-0006)."""

import threading

from openinference.instrumentation import TraceConfig
from openinference.instrumentation.openai import OpenAIInstrumentor
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from traceblame_py._attributes import EVAL_RUN_ID, TENANT_ID
from traceblame_py._sanitize import SanitizingExporter
from traceblame_py.config import ShimConfig
from traceblame_py.features import RetrievalRegistry
from traceblame_py.markers import ContextProcessor


class ShimError(RuntimeError):
    """The shim cannot guarantee that it owns the only export path."""


class SealedTracerProvider(TracerProvider):
    """A provider that takes no span processor after the shim has set it up."""

    _sealed = False

    def add_span_processor(self, span_processor: SpanProcessor) -> None:
        if self._sealed:
            raise ShimError(
                "the shim's tracer provider is sealed: no other processor may see spans"
            )
        super().add_span_processor(span_processor)

    def seal(self) -> None:
        self._sealed = True


def trace_config() -> TraceConfig:
    """OpenInference masking in-process: hide everything no feature needs (ADR-0006 section 2).
    Every field is explicit, so OPENINFERENCE_* variables cannot turn hiding off."""
    return TraceConfig(
        hide_inputs=True,
        hide_input_messages=True,
        hide_input_text=True,
        hide_input_images=True,
        hide_prompts=True,
        hide_llm_tools=True,
        hide_embeddings_text=True,
        hide_embeddings_vectors=True,
        hide_embedding_vectors=True,
        hide_retrieval_documents=True,
        hide_choices=True,
        # Read in-process for aegis.output.parse_ok and cited_doc_ids, never exported.
        hide_outputs=False,
        hide_output_messages=False,
        hide_output_text=False,
        hide_llm_invocation_parameters=False,
        enable_genai_semconv=False,
    )


class Shim:
    def __init__(
        self,
        provider: SealedTracerProvider,
        registry: RetrievalRegistry,
        instrumentor: OpenAIInstrumentor,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self._instrumentor = instrumentor
        self._closed = False

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        """Flush and stop exporting. Spans started afterwards are dropped."""
        global _active
        with _lock:
            if self._closed:
                return
            self._closed = True
            if _active is self:
                _active = None
        self._instrumentor.uninstrument()
        self.provider.shutdown()


_lock = threading.Lock()
_active: Shim | None = None


def active() -> Shim | None:
    return _active


def instrument(
    *,
    service_name: str,
    service_version: str | None = None,
    config: ShimConfig | None = None,
    exporter: SpanExporter | None = None,
) -> Shim:
    """Set up tracing for the process: the shim's provider becomes the global one, and
    OpenAI calls are instrumented with OpenInference.

    The default exporter is OTLP/HTTP, configured by OTEL_EXPORTER_OTLP_* (endpoint, headers
    such as basic auth). Raises ShimError if a global tracer provider is already set.
    """
    global _active
    config = ShimConfig.from_env() if config is None else config
    if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
        raise ShimError(
            "a global tracer provider is already set; the shim must own the only export path"
        )
    attributes: dict[str, str] = {"service.name": service_name}
    if service_version is not None:
        attributes["service.version"] = service_version
    if config.tenant_id is not None:
        attributes[TENANT_ID] = config.tenant_id
    if config.eval_run_id is not None:
        attributes[EVAL_RUN_ID] = config.eval_run_id

    registry = RetrievalRegistry(config.max_traces)
    provider = SealedTracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(ContextProcessor(config.bundle_digest))
    sink = OTLPSpanExporter() if exporter is None else exporter
    provider.add_span_processor(BatchSpanProcessor(SanitizingExporter(sink, registry)))
    provider.seal()
    trace.set_tracer_provider(provider)
    if trace.get_tracer_provider() is not provider:
        provider.shutdown()
        raise ShimError("a global tracer provider was set concurrently; the shim did not start")

    instrumentor = OpenAIInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=trace_config())
    shim = Shim(provider, registry, instrumentor)
    with _lock:
        _active = shim
    return shim
