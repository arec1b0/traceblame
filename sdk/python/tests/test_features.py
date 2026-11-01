"""aegis.output.parse_ok and aegis.output.cited_doc_ids as ADR-0006 section 4 defines them."""

import json

import pytest

from traceblame_py.features import RetrievalRegistry, cited_doc_ids, parse_ok


def _params(response_format: object | None) -> str:
    params: dict[str, object] = {"model": "qwen2.5:7b"}
    if response_format is not None:
        params["response_format"] = response_format
    return json.dumps(params)


@pytest.mark.parametrize(
    ("output", "want"),
    [('{"a": 1}', True), ("[1, 2]", False), ("not json", False), ("", False), (None, False)],
)
def test_parse_ok_json_object(output: str | None, want: bool) -> None:
    assert parse_ok(_params({"type": "json_object"}), output) is want


_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "step",
        "schema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
}


@pytest.mark.parametrize(
    ("output", "want"),
    [
        ('{"action": "x"}', True),
        ('{"thought": "x"}', False),
        ('{"action": 1}', False),
        ("{", False),
    ],
)
def test_parse_ok_json_schema(output: str, want: bool) -> None:
    assert parse_ok(_params(_SCHEMA), output) is want


def test_parse_ok_json_schema_without_a_schema_only_needs_json() -> None:
    fmt = {"type": "json_schema", "json_schema": {"name": "any"}}
    assert parse_ok(_params(fmt), "[1]") is True
    assert parse_ok(_params(fmt), "nope") is False


@pytest.mark.parametrize(
    "params",
    [
        _params(None),
        _params({"type": "text"}),
        _params("json_object"),
        _params({"type": "json_schema", "json_schema": {"schema": {"type": 12}}}),
        "not json",
        None,
    ],
)
def test_parse_ok_has_no_value_without_a_structured_format(params: str | None) -> None:
    assert parse_ok(params, '{"a": 1}') is None


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("see [cuad-ab-1] and [cuad-ab-2]", ["cuad-ab-1", "cuad-ab-2"]),
        ("first [cuad-ab-2], then cuad-ab-1, again cuad-ab-2", ["cuad-ab-2", "cuad-ab-1"]),
        ("cuad-ab-12 is another id", []),
        ("xcuad-ab-1 and cuad-ab-1_b are not citations", []),
        ("(cuad-ab-1).", ["cuad-ab-1"]),
        ("nothing cited", []),
        ("", []),
        (None, []),
    ],
)
def test_cited_doc_ids(text: str | None, want: list[str]) -> None:
    assert cited_doc_ids(["cuad-ab-1", "cuad-ab-2", "cuad-ab-1"], text) == want


def test_cited_doc_ids_escapes_regex_characters() -> None:
    assert cited_doc_ids(["a.b", "c+d"], "axb c+d") == ["c+d"]


def test_registry_keeps_retrievals_that_ended_before_the_call() -> None:
    reg = RetrievalRegistry(max_traces=4)
    reg.add(1, end_ns=100, ids=["a", "b"])
    reg.add(1, end_ns=300, ids=["c", "a"])
    assert reg.before(1, start_ns=99) is None
    assert reg.before(1, start_ns=100) == ["a", "b"]
    assert reg.before(1, start_ns=301) == ["a", "b", "c"]
    assert reg.before(2, start_ns=1000) is None


def test_registry_evicts_least_recently_used_traces() -> None:
    reg = RetrievalRegistry(max_traces=2)
    reg.add(1, end_ns=1, ids=["a"])
    reg.add(2, end_ns=1, ids=["b"])
    assert reg.before(1, start_ns=5) == ["a"]
    reg.add(3, end_ns=1, ids=["c"])
    assert reg.before(2, start_ns=5) is None
    assert reg.before(1, start_ns=5) == ["a"]
    assert reg.before(3, start_ns=5) == ["c"]


def test_registry_needs_room() -> None:
    with pytest.raises(ValueError, match="max_traces"):
        RetrievalRegistry(max_traces=0)
