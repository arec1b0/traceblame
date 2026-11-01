"""Through the real OpenAI client and OpenInference instrumentation, no content leaves the shim.

The LLM server is an httpx MockTransport (a test double, not eval data). Canaries sit in every
place content can come from: system and user messages, tool definitions, the model's output,
tool-call arguments, and an error body that the client copies into the status and the
exception event. The features computed from that content must still be right.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI, BadRequestError
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceblame_py import Shim, agent_span, retriever_span

from .conftest import dump, exported

CANARY = "CANARY-c0ffee"
MODEL = "qwen2.5:7b"


def _completion(message: dict[str, Any], finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


Handler = Callable[[dict[str, Any]], httpx2.Response]


def _client(handler: Handler) -> AsyncOpenAI:
    def route(request: httpx2.Request) -> httpx2.Response:
        return handler(json.loads(request.content))

    transport = httpx2.MockTransport(route)
    return AsyncOpenAI(
        api_key="test",
        base_url="http://llm.invalid/v1",
        http_client=httpx2.AsyncClient(transport=transport),
        max_retries=0,
    )


def _messages() -> list[Any]:
    return [
        {"role": "system", "content": f"system {CANARY}"},
        {"role": "user", "content": f"user {CANARY}"},
    ]


def _llm(spans: list[ReadableSpan]) -> list[ReadableSpan]:
    return [s for s in spans if (s.attributes or {}).get("openinference.span.kind") == "LLM"]


def test_structured_output_and_citations(shim: Shim, exporter: InMemorySpanExporter) -> None:
    answer = json.dumps({"action": "final_answer", "action_input": {"answer": f"{CANARY} [d-2]"}})
    client = _client(
        lambda _: httpx2.Response(200, json=_completion({"role": "assistant", "content": answer}))
    )

    async def run() -> None:
        with agent_span("RAGAgent"):
            with retriever_span("chroma") as r:
                r.set_documents(["d-1", "d-2"], [0.9, 0.8])
            await client.chat.completions.create(
                model=MODEL,
                messages=_messages(),
                response_format={"type": "json_object"},
                max_tokens=64,
                temperature=0.2,
            )

    asyncio.run(run())
    spans = exported(shim, exporter)
    assert CANARY not in dump(spans)
    (llm,) = _llm(spans)
    attrs = dict(llm.attributes or {})
    assert attrs["aegis.output.parse_ok"] is True
    assert attrs["aegis.output.cited_doc_ids"] == ("d-2",)
    assert attrs["gen_ai.request.model"] == MODEL
    assert attrs["gen_ai.request.max_tokens"] == 64
    assert attrs["llm.model_name"] == MODEL
    assert attrs["llm.token_count.prompt"] == 11
    assert attrs["llm.finish_reason"] == "stop"
    assert "llm.invocation_parameters" not in attrs
    assert not [k for k in attrs if k.startswith(("input.", "output.value", "llm.output_messages"))]


def test_a_call_without_retrieval_or_format_gets_neither_feature(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    client = _client(
        lambda _: httpx2.Response(200, json=_completion({"role": "assistant", "content": CANARY}))
    )
    asyncio.run(client.chat.completions.create(model=MODEL, messages=_messages()))
    (llm,) = _llm(exported(shim, exporter))
    attrs = dict(llm.attributes or {})
    assert "aegis.output.parse_ok" not in attrs
    assert "aegis.output.cited_doc_ids" not in attrs


def test_a_retrieval_after_the_call_does_not_count(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    client = _client(
        lambda _: httpx2.Response(200, json=_completion({"role": "assistant", "content": "[d-1]"}))
    )

    async def run() -> None:
        with agent_span("a"):
            await client.chat.completions.create(model=MODEL, messages=_messages())
            with retriever_span("chroma") as r:
                r.set_documents(["d-1"], [0.9])

    asyncio.run(run())
    (llm,) = _llm(exported(shim, exporter))
    assert "aegis.output.cited_doc_ids" not in (llm.attributes or {})


@pytest.mark.parametrize(
    ("response_format", "content", "want"),
    [
        ({"type": "json_object"}, f"not json {CANARY}", False),
        ({"type": "json_object"}, f'["{CANARY}"]', False),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "step",
                    "schema": {"type": "object", "required": ["action"]},
                },
            },
            json.dumps({"thought": CANARY}),
            False,
        ),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "step",
                    "schema": {"type": "object", "required": ["action"]},
                },
            },
            json.dumps({"action": CANARY}),
            True,
        ),
    ],
)
def test_parse_ok_follows_the_requested_format(
    shim: Shim,
    exporter: InMemorySpanExporter,
    response_format: dict[str, Any],
    content: str,
    want: bool,
) -> None:
    client = _client(
        lambda _: httpx2.Response(200, json=_completion({"role": "assistant", "content": content}))
    )
    asyncio.run(
        client.chat.completions.create(
            model=MODEL,
            messages=_messages(),
            response_format=response_format,  # pyright: ignore[reportArgumentType]
        )
    )
    spans = exported(shim, exporter)
    assert CANARY not in dump(spans)
    (llm,) = _llm(spans)
    assert (llm.attributes or {})["aegis.output.parse_ok"] is want


def test_tool_definitions_and_tool_call_arguments_stay_in_process(
    shim: Shim, exporter: InMemorySpanExporter
) -> None:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": json.dumps({"city": CANARY})},
    }
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    client = _client(lambda _: httpx2.Response(200, json=_completion(message, "tool_calls")))
    tools: Any = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": f"weather {CANARY}",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    asyncio.run(client.chat.completions.create(model=MODEL, messages=_messages(), tools=tools))
    spans = exported(shim, exporter)
    assert CANARY not in dump(spans)
    assert "get_weather" not in dump(spans)


def test_an_error_body_is_not_exported(shim: Shim, exporter: InMemorySpanExporter) -> None:
    body = {"error": {"message": f"[] is too short - {CANARY}", "type": "invalid_request_error"}}
    client = _client(lambda _: httpx2.Response(400, json=body))
    with pytest.raises(BadRequestError):
        asyncio.run(
            client.chat.completions.create(
                model=MODEL, messages=_messages(), response_format={"type": "json_object"}
            )
        )
    spans = exported(shim, exporter)
    assert CANARY not in dump(spans)
    (llm,) = _llm(spans)
    attrs = dict(llm.attributes or {})
    assert llm.status.status_code is StatusCode.ERROR
    assert llm.status.description == "BadRequestError: Error code: 400"
    assert [(e.name, dict(e.attributes or {})) for e in llm.events] == [
        ("exception", {"exception.type": "openai.BadRequestError"})
    ]
    assert attrs["gen_ai.request.model"] == MODEL
    assert "aegis.output.parse_ok" not in attrs
