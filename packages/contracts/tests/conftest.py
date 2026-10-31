import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceblame_contracts.fixtures import sha256_file

_MINIMAL_PROFILE: dict[str, Any] = {
    "profile": "aegis-telemetry",
    "major": 1,
    "revision": "1.0.0",
    "semconv": {
        "repository": "open-telemetry/semantic-conventions",
        "version": "1.41.1",
        "commit": "ead83b9b0fa36540c1642fce46e874f002ac23f1",
        "schema_url": "https://opentelemetry.io/schemas/1.41.1",
    },
    "span_kinds": {
        "llm": {"description": "Model inference.", "operations": ["chat"]},
        "chain": {"description": "Any other step.", "operations": []},
    },
    "attributes": [
        {
            "name": "aegis.tenant.id",
            "scope": "resource",
            "type": "string",
            "source": {"aegis": "extension"},
            "requirement": {"all": "required"},
            "brief": "Tenant.",
        },
        {
            "name": "aegis.profile.version",
            "scope": "span",
            "type": "string",
            "const": "aegis-telemetry/1",
            "source": {"aegis": "extension"},
            "requirement": {"all": "required"},
            "brief": "Profile id.",
        },
        {
            "name": "aegis.span.kind",
            "scope": "span",
            "type": "string",
            "enum": ["llm", "chain"],
            "source": {"aegis": "extension", "derived_from": ["gen_ai.operation.name"]},
            "requirement": {"all": "required"},
            "brief": "Span class.",
        },
        {
            "name": "gen_ai.operation.name",
            "scope": "span",
            "type": "string",
            "source": {"semconv": "gen_ai.operation.name"},
            "requirement": {"llm": "required"},
            "brief": "Operation.",
        },
        {
            "name": "gen_ai.request.model",
            "scope": "span",
            "type": "string",
            "source": {"semconv": "gen_ai.request.model"},
            "requirement": {"llm": "required"},
            "brief": "Requested model.",
        },
        {
            "name": "gen_ai.usage.input_tokens",
            "scope": "span",
            "type": "int",
            "source": {"semconv": "gen_ai.usage.input_tokens"},
            "requirement": {"llm": "conditionally_required"},
            "condition": "when the provider reports it",
            "brief": "Input tokens.",
        },
    ],
    "forbidden": [{"name": "gen_ai.input.messages", "reason": "raw prompt content"}],
}

_LLM_SPAN: dict[str, Any] = {
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "span_id": "00f067aa0ba902b7",
    "parent_span_id": "",
    "name": "chat qwen2.5:7b",
    "kind": "SPAN_KIND_CLIENT",
    "start_time_unix_nano": 1_761_840_000_000_000_000,
    "end_time_unix_nano": 1_761_840_001_000_000_000,
    "status": {"code": "STATUS_CODE_UNSET"},
    "resource": {"aegis.tenant.id": "tenant-a", "service.name": "runbook-agent"},
    "scope": {"name": "opentelemetry.instrumentation.openai_v2"},
    "attributes": {
        "aegis.profile.version": "aegis-telemetry/1",
        "aegis.span.kind": "llm",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "qwen2.5:7b",
    },
}


@pytest.fixture
def minimal_profile() -> dict[str, Any]:
    """A small valid profile document; each test gets its own copy to mutate."""
    return copy.deepcopy(_MINIMAL_PROFILE)


@pytest.fixture
def llm_span() -> dict[str, Any]:
    """A flattened span that is valid against the minimal profile."""
    return copy.deepcopy(_LLM_SPAN)


def _provenance(fixture_sha256: str) -> dict[str, Any]:
    return {
        "sdk": "openinference",
        "scenario": "chat_basic",
        "captured_at": "2026-10-30T18:00:00Z",
        "capture_script": "tools/fixtures/capture_openinference.py",
        "capture_script_sha256": "0" * 64,
        "capture_common_sha256": "1" * 64,
        "python": "3.12.15",
        "uv": "0.13.6",
        "packages": {"openinference-instrumentation-openai": "0.1.68"},
        "env": {"OPENINFERENCE_HIDE_INPUTS": "true"},
        "model": {
            "name": "qwen2.5:7b",
            "digest": "845dbda0ea48",
            "server": "ollama",
            "server_version": "0.40.5",
        },
        "endpoint": "http://172.22.96.1:11434/v1",
        "collector": "otelcol-contrib 0.164.0",
        "fixture_sha256": fixture_sha256,
    }


WriteFixture = Callable[..., Path]


@pytest.fixture
def write_fixture() -> WriteFixture:
    """Write an export (default: empty) and a matching provenance record into a directory."""

    def write(
        directory: Path, name: str = "chat_basic", otlp: dict[str, Any] | None = None
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        fixture = directory / f"{name}.otlp.json"
        doc: dict[str, Any] = otlp if otlp is not None else {"resourceSpans": []}
        fixture.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        provenance = _provenance(sha256_file(fixture))
        (directory / f"{name}.provenance.yaml").write_text(yaml.safe_dump(provenance), "utf-8")
        return fixture

    return write
