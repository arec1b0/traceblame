"""Shared body of the per-SDK capture scripts: one real OpenAI-client request, exported over OTLP.

Each capture_<sdk>.py script pins its own dependencies (PEP 723 + lockfile) and imports this
module from its own directory. tools/fixtures/capture.py sets the CAPTURE_* variables, which
select the request, and the OTLP endpoint.

The script prints one JSON report line: interpreter, package versions and the outcome of the
request. The outcome never carries prompt or completion text.
"""

import json
import os
import platform
from collections.abc import Callable
from importlib.metadata import version
from typing import Any

from openai import BadRequestError, OpenAI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

_WEATHER_TOOL: Any = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _chat_params() -> dict[str, Any]:
    return {
        "model": os.environ["CAPTURE_MODEL"],
        "temperature": 0,
        "seed": int(os.environ["CAPTURE_SEED"]),
        "max_tokens": int(os.environ["CAPTURE_MAX_TOKENS"]),
    }


def _user(prompt: str) -> list[Any]:
    return [{"role": "user", "content": prompt}]


def _chat_basic(client: OpenAI) -> dict[str, Any]:
    resp = client.chat.completions.create(
        messages=_user(os.environ["CAPTURE_PROMPT"]), **_chat_params()
    )
    return {"finish_reason": resp.choices[0].finish_reason}


def _chat_tool_call(client: OpenAI) -> dict[str, Any]:
    resp = client.chat.completions.create(
        messages=_user(os.environ["CAPTURE_PROMPT"]), tools=[_WEATHER_TOOL], **_chat_params()
    )
    calls = resp.choices[0].message.tool_calls or []
    return {
        "finish_reason": resp.choices[0].finish_reason,
        "tool_calls": [c.function.name for c in calls if c.type == "function"],
    }


def _chat_error(client: OpenAI) -> dict[str, Any]:
    # An orchestration bug: the conversation is empty. The server answers HTTP 400.
    try:
        client.chat.completions.create(messages=[], **_chat_params())
    except BadRequestError as exc:
        return {"error": type(exc).__name__, "status_code": exc.status_code}
    return {"error": None}


def _embeddings_basic(client: OpenAI) -> dict[str, Any]:
    resp = client.embeddings.create(
        model=os.environ["CAPTURE_MODEL"], input=os.environ["CAPTURE_PROMPT"]
    )
    return {"embeddings": len(resp.data), "dimensions": len(resp.data[0].embedding)}


REQUESTS: dict[str, Callable[[OpenAI], dict[str, Any]]] = {
    "chat_basic": _chat_basic,
    "chat_tool_call": _chat_tool_call,
    "chat_error": _chat_error,
    "embeddings_basic": _embeddings_basic,
}


def run(instrument: Callable[[TracerProvider], object], packages: tuple[str, ...]) -> None:
    provider = TracerProvider(
        resource=Resource.create({"service.name": "traceblame-fixture-capture"})
    )
    # The endpoint comes from OTEL_EXPORTER_OTLP_ENDPOINT, set by capture.py.
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    instrument(provider)

    # Ollama's OpenAI-compatible API ignores the key, but the client requires one.
    client = OpenAI(base_url=os.environ["CAPTURE_OPENAI_BASE_URL"], api_key="ollama")
    outcome = REQUESTS[os.environ["CAPTURE_REQUEST"]](client)
    provider.shutdown()

    report = {
        "python": platform.python_version(),
        "packages": {name: version(name) for name in packages},
        "outcome": outcome,
    }
    print(json.dumps(report))
