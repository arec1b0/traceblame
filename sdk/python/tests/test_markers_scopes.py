"""Correlation ids and eval/fault markers land exactly where the profile puts them."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceblame_py import Shim, ShimConfig, bundle, decision, session, set_fault_injection_id

from .conftest import by_name, exported

_DIGEST = "sha256:" + "ab" * 32
RESOURCE_ONLY = ("aegis.tenant.id", "aegis.eval.run.id")


def test_tenant_and_eval_run_are_resource_attributes_only(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    with trace.get_tracer("t").start_as_current_span("root"):
        pass
    (span,) = exported(shim, exporter)
    assert span.resource.attributes["aegis.tenant.id"] == "tenant-a"
    assert span.resource.attributes["aegis.eval.run.id"] == "run-1"
    assert span.resource.attributes["service.name"] == "shim-test"
    assert not set(RESOURCE_ONLY) & set(span.attributes or {})


def test_session_decision_and_bundle_are_span_attributes_inside_their_context(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    tracer = trace.get_tracer("t")
    with (
        session("s-1"),
        bundle(_DIGEST),
        tracer.start_as_current_span("inside"),
        decision("d-7"),
        tracer.start_as_current_span("decided"),
    ):
        pass
    with tracer.start_as_current_span("outside"):
        pass
    spans = exported(shim, exporter)
    inside = dict(by_name(spans, "inside").attributes or {})
    decided = dict(by_name(spans, "decided").attributes or {})
    outside = dict(by_name(spans, "outside").attributes or {})
    assert inside["aegis.session.id"] == "s-1"
    assert inside["aegis.bundle.digest"] == _DIGEST
    assert "aegis.decision.id" not in inside
    assert decided["aegis.decision.id"] == "d-7"
    assert decided["aegis.session.id"] == "s-1"
    assert not {"aegis.session.id", "aegis.bundle.digest", "aegis.decision.id"} & set(outside)
    for s in spans:
        assert not set(RESOURCE_ONLY) & set(s.attributes or {})
        assert "aegis.session.id" not in s.resource.attributes


def test_config_bundle_applies_to_every_span(exporter: InMemorySpanExporter) -> None:
    from traceblame_py import instrument

    from .conftest import reset_global_provider

    reset_global_provider()
    s = instrument(service_name="b", config=ShimConfig(bundle_digest=_DIGEST), exporter=exporter)
    try:
        with trace.get_tracer("t").start_as_current_span("x"):
            pass
        (span,) = exported(s, exporter)
        assert (span.attributes or {})["aegis.bundle.digest"] == _DIGEST
        assert "aegis.eval.run.id" not in span.resource.attributes
    finally:
        s.shutdown()
        reset_global_provider()


def test_fault_injection_id_marks_only_its_span(shim: Shim, exporter: InMemorySpanExporter) -> None:
    tracer = trace.get_tracer("t")
    with tracer.start_as_current_span("parent"), tracer.start_as_current_span("target"):
        set_fault_injection_id("inj-01:retrieval")
        with tracer.start_as_current_span("child"):
            pass
    spans = exported(shim, exporter)
    assert (by_name(spans, "target").attributes or {})[
        "aegis.fault.injection.id"
    ] == "inj-01:retrieval"
    for name in ("parent", "child"):
        assert "aegis.fault.injection.id" not in (by_name(spans, name).attributes or {})
    assert all("aegis.fault.injection.id" not in s.resource.attributes for s in spans)


def test_fault_injection_id_on_an_explicit_span(shim: Shim, exporter: InMemorySpanExporter) -> None:
    span = trace.get_tracer("t").start_span("explicit")
    set_fault_injection_id("inj-2", span=span)
    span.end()
    (out,) = exported(shim, exporter)
    assert (out.attributes or {})["aegis.fault.injection.id"] == "inj-2"


def test_fault_injection_id_needs_a_recording_span(shim: Shim) -> None:
    with pytest.raises(ValueError, match="recording span"):
        set_fault_injection_id("inj-3")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"eval_run_id": "bad id"},
        {"eval_run_id": "x" * 129},
        {"eval_run_id": ""},
        {"bundle_digest": "sha256:xyz"},
        {"bundle_digest": "ab" * 32},
        {"tenant_id": ""},
        {"max_traces": 0},
    ],
)
def test_invalid_config_raises(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case names its own field
        ShimConfig(**kwargs)  # pyright: ignore[reportArgumentType]


def test_invalid_marker_values_raise(shim: Shim) -> None:
    with pytest.raises(ValueError, match=r"aegis\.fault\.injection\.id"):
        set_fault_injection_id("x" * 129)
    with pytest.raises(ValueError, match=r"aegis\.bundle\.digest"), bundle("sha256:nope"):
        pass
    with pytest.raises(ValueError, match=r"aegis\.session\.id"), session(""):
        pass
    with pytest.raises(ValueError, match=r"aegis\.decision\.id"), decision(""):
        pass


def test_config_from_env() -> None:
    env = {
        "TRACEBLAME_TENANT_ID": "tenant-b",
        "TRACEBLAME_EVAL_RUN_ID": "rag-cuad-20261031-s7-abc1234",
        "TRACEBLAME_BUNDLE_DIGEST": _DIGEST,
        "TRACEBLAME_MAX_TRACES": "16",
    }
    assert ShimConfig.from_env(env) == ShimConfig(
        tenant_id="tenant-b",
        eval_run_id="rag-cuad-20261031-s7-abc1234",
        bundle_digest=_DIGEST,
        max_traces=16,
    )
    assert ShimConfig.from_env({}) == ShimConfig()
