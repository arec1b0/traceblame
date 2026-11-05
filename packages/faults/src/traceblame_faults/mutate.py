"""Rewrites of OpenAI-compatible chat-completions bodies at the LLM seam: pure functions on
bytes, shared with the TypeScript preload through vectors/mutations.json.

Requests: add fields (`seed`, `max_tokens`), replace the system message (prompt variant).
Responses: malform the first choice's message content (truncate, fence, prose) and leave
everything else, finish_reason included, as the server returned it.
"""

import json
import math
from collections.abc import Mapping
from typing import Any, cast

MODES = ("truncate", "fence", "prose")
FENCE = ("```json\n", "\n```")
PROSE = "Here is my next step:\n"


class MutationError(ValueError):
    pass


def _obj(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise MutationError("body is not JSON") from exc
    if not isinstance(data, dict):
        raise MutationError("body is not a JSON object")
    return cast("dict[str, Any]", data)


def _bytes(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def with_fields(body: bytes, fields: Mapping[str, Any]) -> bytes:
    return _bytes(_obj(body) | dict(fields))


def _system_index(data: Mapping[str, Any]) -> int | None:
    messages = data.get("messages")
    if not isinstance(messages, list):
        return None
    for i, m in enumerate(cast("list[Any]", messages)):
        if isinstance(m, dict) and cast("dict[str, Any]", m).get("role") == "system":
            return i
    return None


def system_text(body: bytes) -> str | None:
    try:
        data = _obj(body)
    except MutationError:
        return None
    i = _system_index(data)
    if i is None:
        return None
    content = data["messages"][i].get("content")
    return content if isinstance(content, str) else None


def with_system(body: bytes, text: str) -> bytes:
    data = _obj(body)
    i = _system_index(data)
    if i is None:
        raise MutationError("request has no system message")
    messages = list(data["messages"])
    messages[i] = dict(messages[i]) | {"content": text}
    return _bytes(data | {"messages": messages})


def content(body: bytes) -> str:
    data = _obj(body)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MutationError("response has no choices")
    first = cast("list[dict[str, Any]]", choices)[0]
    message = cast("dict[str, Any]", first.get("message") or {})
    text = message.get("content")
    if not isinstance(text, str):
        raise MutationError("first choice has no text content")
    return text


def parses_as_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _truncate(text: str, cut_fraction: float) -> str:
    points = list(text)  # code points, as the preload's Array.from
    cut = max(1, math.floor(len(points) * cut_fraction))
    out = "".join(points[:cut])
    while cut > 0 and parses_as_object(out):
        cut -= 1
        out = "".join(points[:cut])
    return out


def malform(body: bytes, mode: str, cut_fraction: float) -> bytes:
    if mode not in MODES:
        raise MutationError(f"mode {mode!r} is not one of {MODES}")
    data = _obj(body)
    text = content(body)
    if mode == "truncate":
        new = _truncate(text, cut_fraction)
    elif mode == "fence":
        new = FENCE[0] + text + FENCE[1]
    else:
        new = PROSE + text
    choices = list(data["choices"])
    first = dict(choices[0])
    first["message"] = dict(first["message"]) | {"content": new}
    choices[0] = first
    return _bytes(data | {"choices": choices})
