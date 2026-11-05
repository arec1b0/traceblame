"""Request and response rewrites at the LLM seam: the golden vectors are shared with the
TypeScript preload (vectors/mutations.json), so both sides inject the same fault."""

import json
from pathlib import Path
from typing import Any

import pytest

from traceblame_faults import bundle, ids, mutate

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors" / "mutations.json").read_text(encoding="utf-8")
)


def _b(obj: Any) -> bytes:
    return json.dumps(obj).encode()


def _response(content: str, finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


@pytest.mark.parametrize("vector", VECTORS["with_fields"], ids=lambda v: v["name"])
def test_with_fields_vectors(vector: dict[str, Any]) -> None:
    assert json.loads(mutate.with_fields(_b(vector["body"]), vector["fields"])) == vector["want"]


@pytest.mark.parametrize("vector", VECTORS["with_system"], ids=lambda v: v["name"])
def test_with_system_vectors(vector: dict[str, Any]) -> None:
    out = mutate.with_system(_b(vector["body"]), vector["text"])
    assert json.loads(out) == vector["want"]
    assert mutate.system_text(out) == vector["text"]


@pytest.mark.parametrize("vector", VECTORS["malform"], ids=lambda v: v["name"])
def test_malform_vectors(vector: dict[str, Any]) -> None:
    out = mutate.malform(_b(vector["body"]), vector["mode"], vector["cut_fraction"])
    assert json.loads(out) == vector["want"]
    assert not mutate.parses_as_object(mutate.content(out))


@pytest.mark.parametrize("vector", VECTORS["request_seed"], ids=lambda v: str(v["args"]))
def test_request_seed_vectors(vector: dict[str, Any]) -> None:
    seed, ref, call = vector["args"]
    assert ids.request_seed(seed, ref, call) == vector["want"]


@pytest.mark.parametrize("vector", VECTORS["digest"], ids=lambda v: v["name"])
def test_digest_vectors(vector: dict[str, Any]) -> None:
    assert bundle.digest(vector["app"], vector["templates"]) == vector["want"]


def test_the_finish_reason_is_left_as_returned() -> None:
    out = json.loads(mutate.malform(_b(_response('{"action": "x"}')), "truncate", 0.5))
    assert out["choices"][0]["finish_reason"] == "stop"


def test_truncation_never_leaves_a_parsable_object() -> None:
    # A content whose prefix "{}" parses: the cut moves left until nothing parses.
    out = mutate.malform(_b(_response('{} {"a": 1}')), "truncate", 0.3)
    assert not mutate.parses_as_object(mutate.content(out))


def test_truncation_cuts_at_code_points() -> None:
    out = mutate.malform(_b(_response('{"a": "ü€😀"}')), "truncate", 0.5)
    assert mutate.content(out) == '{"a": '


@pytest.mark.parametrize(
    ("fn", "match"),
    [
        (
            lambda: mutate.with_system(_b({"messages": [{"role": "user", "content": "x"}]}), "t"),
            "system",
        ),
        (lambda: mutate.malform(_b({"choices": []}), "truncate", 0.5), "choices"),
        (lambda: mutate.malform(_b(_response("x")), "shout", 0.5), "mode"),
        (lambda: mutate.with_fields(b"not json", {"seed": 1}), "JSON"),
        (lambda: mutate.with_fields(_b([1]), {"seed": 1}), "object"),
    ],
)
def test_errors(fn: Any, match: str) -> None:
    with pytest.raises(mutate.MutationError, match=match):
        fn()


def test_parses_as_object() -> None:
    assert mutate.parses_as_object('{"a": 1}')
    assert not mutate.parses_as_object("[1]")
    assert not mutate.parses_as_object('```json\n{"a": 1}\n```')


def test_system_text_is_none_without_a_system_message() -> None:
    assert mutate.system_text(_b({"messages": []})) is None
    assert mutate.system_text(b"{}") is None
