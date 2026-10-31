import json
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once

from traceblame_py import Shim, ShimConfig, instrument


def reset_global_provider() -> None:
    """OTel sets the global provider once per process; tests need one shim each."""
    trace._TRACER_PROVIDER_SET_ONCE = Once()  # pyright: ignore[reportPrivateUsage]
    trace._TRACER_PROVIDER = None  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def shim(exporter: InMemorySpanExporter) -> Iterator[Shim]:
    reset_global_provider()
    s = instrument(
        service_name="shim-test",
        config=ShimConfig(tenant_id="tenant-a", eval_run_id="run-1"),
        exporter=exporter,
    )
    yield s
    s.shutdown()
    reset_global_provider()


def exported(shim: Shim, exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    shim.force_flush()
    return list(exporter.get_finished_spans())


def by_name(spans: Sequence[ReadableSpan], name: str) -> ReadableSpan:
    found = [s for s in spans if s.name == name]
    assert len(found) == 1, [s.name for s in spans]
    return found[0]


def dump(spans: Sequence[ReadableSpan]) -> str:
    """Everything an exporter could send: names, attributes, events, links, status, resource."""
    out: list[dict[str, Any]] = []
    for s in spans:
        out.append(
            {
                "name": s.name,
                "attributes": dict(s.attributes or {}),
                "events": [(e.name, dict(e.attributes or {})) for e in s.events],
                "links": [dict(link.attributes or {}) for link in s.links],
                "status": [s.status.status_code.name, s.status.description],
                "resource": dict(s.resource.attributes),
            }
        )
    return json.dumps(out, default=str)
