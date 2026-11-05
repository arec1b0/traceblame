"""The six fault kinds (ADR draft "Fault taxonomy, injection points and eval ground truth").

Each kind has a ground-truth component, the apps it is injected into, the span its marker
(aegis.fault.injection.id) goes on, and a closed parameter schema. injections.params holds only
numbers, booleans, enum values, ids and sha256 digests: never prompt or document text.
"""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from traceblame_faults.ids import PATTERN as ID_PATTERN

COMPONENTS = ("retrieval", "prompt", "tool", "model", "orchestration")
APPS = ("agentic-rag", "runbook-agent")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

ParamType = Literal["enum", "int", "float", "id", "digest"]


class ParamsError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    type: ParamType
    values: tuple[str, ...] = ()
    lo: float = -math.inf
    hi: float = math.inf
    # Required in a plan. Applied-only fields are filled by the injector when the fault fires.
    required: bool = True
    applied_only: bool = False


@dataclass(frozen=True)
class Kind:
    name: str
    component: str
    apps: frozenset[str]
    marker: Mapping[str, str]  # app -> span kind the marker goes on
    signal: str  # what the #16 detectors are expected to see move
    params: Mapping[str, Param] = field(default_factory=dict[str, Param])


_CALL = Param("int", lo=1, hi=20)
_PERSIST = Param("enum", values=("once", "all"))
COMMON: Mapping[str, Param] = {
    "scenario": Param("id"),
    "scenario_version": Param("int", lo=1, hi=1000),
    "faults_version": Param("id"),
    # The run's baseline prompt digest (the app's own templates), for prompt_regression (#16).
    "baseline_digest": Param("digest", required=False, applied_only=True),
}

KINDS: Mapping[str, Kind] = {
    k.name: k
    for k in (
        Kind(
            "golden_context_removed",
            "retrieval",
            frozenset({"agentic-rag"}),
            {"agentic-rag": "retriever"},
            "retrieval_miss: lower max retrieval score; golden_in_topk (eval-only) false",
            {
                "mode": Param("enum", values=("remove", "distractor")),
                "top_k": Param("int", lo=1, hi=50),
                "removed_n": Param("int", lo=0, hi=50, required=False, applied_only=True),
                "retrievals": Param("int", lo=0, hi=50, required=False, applied_only=True),
                "distractor_doc": Param("id", required=False, applied_only=True),
            },
        ),
        Kind(
            "tool_failure",
            "tool",
            frozenset({"agentic-rag"}),
            {"agentic-rag": "tool"},
            "tool_error_propagation: an ERROR tool span consumed by the next LLM step",
            {
                "mode": Param("enum", values=("backend_error", "backend_timeout")),
                "tool": Param("enum", values=("retriever",)),
                "call_index": _CALL,
            },
        ),
        Kind(
            "malformed_output",
            "model",
            frozenset({"agentic-rag", "runbook-agent"}),
            {"agentic-rag": "llm", "runbook-agent": "llm"},
            "schema_violation: aegis.output.parse_ok=false on the marked LLM span",
            {
                "mode": Param("enum", values=("truncate", "fence", "prose")),
                "cut_fraction": Param("float", lo=0.05, hi=0.95),
                "call_index": _CALL,
                "persist": _PERSIST,
            },
        ),
        Kind(
            "output_budget_exhausted",
            "orchestration",
            frozenset({"agentic-rag"}),
            {"agentic-rag": "llm"},
            "context_truncation: finish_reason=length (parse_ok=false follows)",
            {
                "max_tokens": Param("int", lo=1, hi=256),
                "call_index": _CALL,
                "persist": _PERSIST,
            },
        ),
        Kind(
            "step_memory_loss",
            "orchestration",
            frozenset({"agentic-rag"}),
            {"agentic-rag": "agent"},
            "loop: the same (tool_name, args_hash) at least 3 times in the agent span",
            {"from_iteration": Param("int", lo=2, hi=10)},
        ),
        Kind(
            "prompt_variant",
            "prompt",
            frozenset({"agentic-rag", "runbook-agent"}),
            {"agentic-rag": "agent", "runbook-agent": "llm"},
            "prompt_regression (proposed MVP, #16): bundle.digest differs from the baseline",
            {
                "variant": Param("id"),
                "bundle_digest": Param("digest", required=False, applied_only=True),
            },
        ),
    )
}


def _check(name: str, spec: Param, value: Any) -> None:
    if spec.type == "enum":
        ok = isinstance(value, str) and value in spec.values
    elif spec.type == "int":
        ok = type(value) is int and spec.lo <= value <= spec.hi
    elif spec.type == "float":
        ok = type(value) in (int, float) and math.isfinite(value) and spec.lo <= value <= spec.hi
    elif spec.type == "id":
        ok = isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None
    else:
        ok = isinstance(value, str) and DIGEST_PATTERN.fullmatch(value) is not None
    if not ok:
        raise ParamsError(f"param {name}={value!r} is not a valid {spec.type}")


def schema(kind: str) -> Mapping[str, Param]:
    if kind not in KINDS:
        raise ParamsError(f"unknown fault kind {kind!r}")
    return dict(COMMON) | dict(KINDS[kind].params)


def check_param(kind: str, name: str, value: Any) -> None:
    spec = schema(kind).get(name)
    if spec is None:
        raise ParamsError(f"{kind}: unknown param {name!r}")
    _check(name, spec, value)


def check_params(kind: str, params: Mapping[str, Any], *, applied: bool = False) -> None:
    """A closed schema: every key known, every value of its type, required keys present.
    applied=False checks a plan, which may not hold the applied-only fields yet."""
    specs = schema(kind)
    for name, value in params.items():
        spec = specs.get(name)
        if spec is None:
            raise ParamsError(f"{kind}: unknown param {name!r}")
        if spec.applied_only and not applied:
            raise ParamsError(f"{kind}: param {name!r} is set only when the fault is applied")
        _check(name, spec, value)
    missing = [n for n, s in specs.items() if s.required and n not in params]
    if missing:
        raise ParamsError(f"{kind}: missing params {missing}")


def params_json(params: Mapping[str, Any]) -> str:
    """injections.params: compact, keys sorted."""
    return json.dumps(dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
