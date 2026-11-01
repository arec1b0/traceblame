"""aegis.tool.args_hash: the Python shim and the Go normalizer agree on generated arguments.

The golden vectors pin chosen cases; this compares many generated ones, valid and not
I-JSON, from a fixed seed. All go through the normalizer in one OTLP request, as tool spans
carrying gen_ai.tool.call.arguments, which it hashes when no hash is set (ADR-0002 section 2).
"""

import json
import os
import random
from pathlib import Path
from typing import Any

import pytest

from traceblame_contracts.normalizers import (
    BINARY_ENV,
    CONFIG_ENV,
    DEFAULT_GO_BINARY,
    DEFAULT_GO_CONFIG,
    GoNormalizer,
)
from traceblame_contracts.otlp import flatten_otlp_json
from traceblame_py import args_hash
from traceblame_py._jcs import NotIJSONError, canonicalize

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.contract

SEED = 12
CASES = 600
# Built with chr() so that the code points stay visible and no formatter turns them literal.
_KEYS = [
    "a",
    "b",
    "city",
    "units",
    "A",
    "aa",
    "",
    "1",
    "\r",
    *map(chr, (0x20AC, 0x1F600, 0xFB33, 0xF6)),
]
_STRINGS = [
    "",
    "Paris",
    "line\nbreak",
    'q"uote',
    "back\\slash",
    *map(chr, (0x07, 0x2028, 0xE9, 0x1F600)),
]


def _value(rng: random.Random, depth: int) -> Any:
    kind = rng.randrange(8 if depth < 4 else 5)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind == 1:
        return rng.randint(-(2**70), 2**70)
    if kind == 2:
        return rng.choice([0.0, -0.0, 1e21, 1e-7, 5e-324, 1.7976931348623157e308]) * rng.choice(
            [1, -1]
        )
    if kind == 3:
        return rng.uniform(-1e6, 1e6) * 10 ** rng.randint(-30, 30)
    if kind == 4:
        return rng.choice(_STRINGS) + rng.choice(_STRINGS)
    if kind in (5, 6):
        return {rng.choice(_KEYS): _value(rng, depth + 1) for _ in range(rng.randrange(5))}
    return [_value(rng, depth + 1) for _ in range(rng.randrange(5))]


def _text(rng: random.Random) -> str:
    text = json.dumps(
        _value(rng, 0),
        ensure_ascii=rng.random() < 0.5,
        indent=rng.choice([None, 0, 2]),
        separators=rng.choice([None, (",", ":"), (" , ", " : ")]),
    )
    mutation = rng.randrange(10)
    if mutation == 0 and len(text) > 1:
        return text[: rng.randrange(1, len(text))]  # truncated
    if mutation == 1:
        return text + rng.choice([" {}", "x", " 1"])  # trailing data
    if mutation == 2 and text.startswith("{") and text != "{}":
        first = text[1 : text.index(":")]
        return "{" + first + ": 0, " + text[1:]  # duplicate property
    if mutation == 3:
        return text.replace("1", "1e400", 1)  # number out of range, or plain text
    return text


def _request(args: list[str]) -> dict[str, Any]:
    spans = [
        {
            "traceId": "0" * 31 + "1",
            "spanId": f"{i + 1:016x}",
            "name": "tool",
            "kind": 1,
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": "t"}},
                {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": a}},
            ],
        }
        for i, a in enumerate(args)
    ]
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "parity"}}]}
    return {
        "resourceSpans": [
            {"resource": resource, "scopeSpans": [{"scope": {"name": "parity"}, "spans": spans}]}
        ]
    }


def test_python_and_go_hash_generated_arguments_alike() -> None:
    rng = random.Random(SEED)  # noqa: S311 - reproducible test inputs, not secrets
    args = [_text(rng) for _ in range(CASES)]
    go = GoNormalizer(
        Path(os.environ.get(BINARY_ENV, ROOT / DEFAULT_GO_BINARY)),
        Path(os.environ.get(CONFIG_ENV, ROOT / DEFAULT_GO_CONFIG)),
    )
    spans = flatten_otlp_json(go.normalize(_request(args)))
    got = {int(s["span_id"], 16) - 1: s["attributes"]["aegis.tool.args_hash"] for s in spans}
    assert len(got) == CASES
    mismatches = [
        (args[i], got[i], args_hash(args[i])) for i in range(CASES) if got[i] != args_hash(args[i])
    ]
    assert mismatches == []

    # The run must exercise both paths, or it proves little.
    def _ijson(text: str) -> bool:
        try:
            canonicalize(text)
        except NotIJSONError:
            return False
        return True

    valid = sum(_ijson(a) for a in args)
    assert CASES * 0.5 < valid < CASES
