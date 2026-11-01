"""The shim owns the only export path (ADR-0006 section 1)."""

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceblame_py import Shim, ShimError, instrument

from .conftest import reset_global_provider


@pytest.fixture
def clean_global() -> Iterator[None]:
    reset_global_provider()
    yield
    reset_global_provider()


@pytest.mark.usefixtures("clean_global")
def test_an_existing_global_provider_is_refused() -> None:
    trace.set_tracer_provider(TracerProvider())
    with pytest.raises(ShimError, match="global tracer provider"):
        instrument(service_name="x", exporter=InMemorySpanExporter())


def test_a_second_instrument_is_refused(shim: Shim) -> None:
    with pytest.raises(ShimError, match="global tracer provider"):
        instrument(service_name="y", exporter=InMemorySpanExporter())


def test_no_processor_can_be_added_after_instrument(shim: Shim) -> None:
    assert trace.get_tracer_provider() is shim.provider
    with pytest.raises(ShimError, match="sealed"):
        shim.provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))


def test_shutdown_is_idempotent(shim: Shim) -> None:
    shim.shutdown()
    shim.shutdown()
