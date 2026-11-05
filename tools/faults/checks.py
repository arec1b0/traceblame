"""Validation checks of an injected trace against the clean trace of the same query (#14).

Pure functions over stored span rows (ClickHouse `spans`, as JSONEachRow dicts). They decide,
per fault kind, whether the signal the #16 detector is expected to read moved. They are not
detectors: they know which trace is faulted and where, and only verify that the injector did
what its kind promises.
"""

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

MARKER = "aegis.fault.injection.id"
# The span the marker goes on (traceblame_faults.kinds marker names) -> spans.span_kind.
SPAN_KIND = {"retriever": "retrieval", "tool": "tool", "llm": "llm", "agent": "agent"}
_ERROR = {"Error", "STATUS_CODE_ERROR"}
# The kinds signal() checks. Each applied injection must move its signal (PR #64): a miss is a
# validation problem, except for the kinds in AGGREGATE_GATED, whose per-trace signal depends on
# the model and is gated by a hit rate over the run instead (validation.yaml, loop.hit_rate_min).
KINDS_WITH_SIGNAL = (
    "golden_context_removed",
    "tool_failure",
    "malformed_output",
    "output_budget_exhausted",
    "step_memory_loss",
    "prompt_variant",
)
AGGREGATE_GATED = frozenset({"step_memory_loss"})


def _list(row: Mapping[str, Any], key: str) -> list[Any]:
    return cast("list[Any]", row.get(key) or [])


def _start(row: Mapping[str, Any]) -> str:
    return str(row["start_ts"])


def _end_ms(row: Mapping[str, Any]) -> float:
    from datetime import datetime

    start = datetime.fromisoformat(str(row["start_ts"]).replace(" ", "T"))
    return start.timestamp() * 1000 + float(row["duration_ms"])


def _start_ms(row: Mapping[str, Any]) -> float:
    from datetime import datetime

    return datetime.fromisoformat(str(row["start_ts"]).replace(" ", "T")).timestamp() * 1000


@dataclass(frozen=True)
class Features:
    spans: int
    max_score: float | None
    golden_in_topk: bool | None  # eval-only: None when the query has no golden chunks
    parse_ok: tuple[str, ...]
    finish_reasons: tuple[str, ...]
    loop_max: int
    tool_errors: int
    tool_errors_consumed: int
    tool_errors_retried: int
    digests: frozenset[str]
    marked: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (span_kind, id)


def features(rows: Sequence[Mapping[str, Any]], golden: Collection[str] = ()) -> Features:
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for r in sorted(rows, key=_start):
        by_kind.setdefault(str(r["span_kind"]), []).append(r)
    retrieval = by_kind.get("retrieval", [])
    scores = [float(s) for r in retrieval for s in _list(r, "retrieval_scores")]
    docs = {str(d) for r in retrieval for d in _list(r, "retrieved_doc_ids")}
    llm = by_kind.get("llm", [])
    tools = by_kind.get("tool", [])
    repeats = Counter((t.get("tool_name"), t.get("args_hash")) for t in tools)
    errors = [t for t in tools if t.get("status_code") in _ERROR]
    consumed = retried = 0
    for t in errors:
        end = _end_ms(t)
        if any(_start_ms(m) >= end for m in llm):
            consumed += 1
        key = (t.get("tool_name"), t.get("args_hash"))
        if any(
            _start_ms(u) >= end and (u.get("tool_name"), u.get("args_hash")) == key for u in tools
        ):
            retried += 1
    return Features(
        spans=len(rows),
        max_score=max(scores) if scores else None,
        golden_in_topk=(bool(docs & set(golden)) if golden else None),
        parse_ok=tuple(str(r["attrs"].get("aegis.output.parse_ok", "")) for r in llm),
        finish_reasons=tuple(str(f) for r in llm for f in _list(r, "finish_reasons")),
        loop_max=max(repeats.values(), default=0),
        tool_errors=len(errors),
        tool_errors_consumed=consumed,
        tool_errors_retried=retried,
        digests=frozenset(str(r["bundle_digest"]) for r in rows if r.get("bundle_digest")),
        marked=tuple(
            (str(r["span_kind"]), str(r["attrs"][MARKER])) for r in rows if MARKER in r["attrs"]
        ),
    )


def marked_rows(rows: Sequence[Mapping[str, Any]], injection_id: str) -> list[Mapping[str, Any]]:
    return [r for r in rows if r["attrs"].get(MARKER) == injection_id]


def retrieval_counted(mode: str | None, clean_golden_in_topk: bool | None) -> bool:
    """Whether a golden_context_removed trace counts as a retrieval fault in D1 (#19): a
    distractor replaces the results whatever they were; "remove" changes the retrieval only
    when the golden set was in the clean run's top-k."""
    return mode == "distractor" or clean_golden_in_topk is True


def signal(
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    faulted: Features,
    clean: Features | None,
    *,
    injection_id: str,
    bundle_digest: str | None = None,
    repeat_min: int = 3,
    mode: str | None = None,
) -> dict[str, Any]:
    """{"moved": bool, ...the values compared}."""
    marked = marked_rows(rows, injection_id)
    if kind == "golden_context_removed":
        lower = (
            faulted.max_score is not None
            and clean is not None
            and clean.max_score is not None
            and faulted.max_score < clean.max_score
        )
        clean_hit = clean is not None and clean.golden_in_topk is True
        return {
            "moved": faulted.golden_in_topk is False and (clean_hit or lower),
            "golden_in_topk": [clean.golden_in_topk if clean else None, faulted.golden_in_topk],
            "max_score": [clean.max_score if clean else None, faulted.max_score],
            "mode": mode,
            "counted": retrieval_counted(mode, clean.golden_in_topk if clean else None),
        }
    if kind == "tool_failure":
        errored = [r for r in marked if r.get("status_code") in _ERROR]
        return {
            "moved": bool(errored) and faulted.tool_errors_consumed > 0,
            "marked_tool_error": len(errored),
            "consumed_by_llm": faulted.tool_errors_consumed,
            "retried": faulted.tool_errors_retried,
            "error_type": sorted({str(r.get("error_type", "")) for r in errored}),
        }
    if kind == "malformed_output":
        values = [str(r["attrs"].get("aegis.output.parse_ok", "")) for r in marked]
        return {
            "moved": "false" in values,
            "parse_ok": [sorted(set(clean.parse_ok)) if clean else None, values],
        }
    if kind == "output_budget_exhausted":
        reasons = [str(f) for r in marked for f in _list(r, "finish_reasons")]
        return {
            "moved": "length" in reasons,
            "finish_reasons": [sorted(set(clean.finish_reasons)) if clean else None, reasons],
            "parse_ok": [str(r["attrs"].get("aegis.output.parse_ok", "")) for r in marked],
        }
    if kind == "step_memory_loss":
        return {
            "moved": faulted.loop_max >= repeat_min,
            "loop_max": [clean.loop_max if clean else None, faulted.loop_max],
            "repeat_min": repeat_min,
        }
    if kind == "prompt_variant":
        clean_digests = sorted(clean.digests) if clean else []
        return {
            "moved": bool(faulted.digests)
            and faulted.digests != (clean.digests if clean else frozenset())
            and (bundle_digest is None or faulted.digests == frozenset({bundle_digest})),
            "bundle_digest": [clean_digests, sorted(faulted.digests)],
        }
    raise ValueError(f"unknown fault kind {kind!r}")


def signal_problem(kind: str, sig: Mapping[str, Any], trace_id: str) -> str | None:
    """The validation problem of an applied injection whose signal did not move, if any."""
    if kind in AGGREGATE_GATED or sig.get("moved"):
        return None
    return f"{trace_id} {kind}: applied, but its signal did not move"


def marker_kinds_ok(
    rows: Sequence[Mapping[str, Any]], injection_id: str, expected: str
) -> tuple[bool, list[str]]:
    """Every span marked with this id is of the expected kind, and there is at least one."""
    kinds_seen = [str(r["span_kind"]) for r in marked_rows(rows, injection_id)]
    return bool(kinds_seen) and all(k == SPAN_KIND[expected] for k in kinds_seen), kinds_seen
