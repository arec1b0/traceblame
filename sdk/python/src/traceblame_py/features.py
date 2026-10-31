"""Content-free features computed in-process (ADR-0006 section 4)."""

import json
import re
import threading
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

_STRUCTURED = ("json_object", "json_schema")
# A citation is the id itself, not part of a longer id.
_ID_CHAR = r"A-Za-z0-9_-"


def parse_ok(invocation_parameters: str | None, output: str | None) -> bool | None:
    """aegis.output.parse_ok of a successful call, or None when it asked for no structured
    output (or for a schema that is not a valid JSON Schema, which cannot be judged)."""
    fmt = _response_format(invocation_parameters)
    if fmt is None:
        return None
    schema: Any = None
    if fmt.get("type") == "json_schema":
        spec = fmt.get("json_schema")
        schema = cast(dict[str, Any], spec).get("schema") if isinstance(spec, dict) else None
        if schema is not None:
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                return None
    if output is None:
        return False
    try:
        value: Any = json.loads(output)
    except ValueError:
        return False
    if fmt.get("type") == "json_object":
        return isinstance(value, dict)
    if schema is None:
        return True
    validator: Any = Draft202012Validator(schema)
    return bool(validator.is_valid(value))


def _response_format(invocation_parameters: str | None) -> dict[str, Any] | None:
    if invocation_parameters is None:
        return None
    try:
        params: Any = json.loads(invocation_parameters)
    except ValueError:
        return None
    if not isinstance(params, dict):
        return None
    fmt: Any = cast(dict[str, Any], params).get("response_format")
    if not isinstance(fmt, dict):
        return None
    typed = cast(dict[str, Any], fmt)
    return typed if typed.get("type") in _STRUCTURED else None


def cited_doc_ids(candidates: Sequence[str], text: str | None) -> list[str]:
    """The candidate ids the text cites, in order of first occurrence, without repeats."""
    if not text:
        return []
    first: dict[str, int] = {}
    for doc_id in dict.fromkeys(candidates):
        pattern = rf"(?<![{_ID_CHAR}]){re.escape(doc_id)}(?![{_ID_CHAR}])"
        found = re.search(pattern, text)
        if found is not None:
            first[doc_id] = found.start()
    return sorted(first, key=first.__getitem__)


class RetrievalRegistry:
    """Ids retrieved per trace, with the time each retrieval ended. Bounded, LRU first out."""

    def __init__(self, max_traces: int) -> None:
        if max_traces < 1:
            raise ValueError("max_traces must be at least 1")
        self._max = max_traces
        self._traces: OrderedDict[int, list[tuple[int, tuple[str, ...]]]] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, trace_id: int, end_ns: int, ids: Sequence[str]) -> None:
        with self._lock:
            self._traces.setdefault(trace_id, []).append((end_ns, tuple(ids)))
            self._traces.move_to_end(trace_id)
            while len(self._traces) > self._max:
                self._traces.popitem(last=False)

    def before(self, trace_id: int, start_ns: int) -> list[str] | None:
        """Ids of the trace's retrievals that ended at or before start_ns, in retrieval order
        and without repeats; None when there were none."""
        with self._lock:
            retrievals = self._traces.get(trace_id)
            if retrievals is None:
                return None
            self._traces.move_to_end(trace_id)
            earlier = [ids for end, ids in retrievals if end <= start_ns]
        if not earlier:
            return None
        return list(dict.fromkeys(i for ids in earlier for i in ids))
