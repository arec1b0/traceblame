"""tools/faults/checks.py: the validation run's per-kind signal checks. The rows are shaped like
ClickHouse `spans` rows (test doubles, never eval data)."""

from typing import Any

import checks  # tools/faults, on pytest's pythonpath as at the driver's run time
import pytest

ID = "run:kind:abc"


def _row(kind: str, start: str, **kw: Any) -> dict[str, Any]:
    return {
        "span_kind": kind,
        "start_ts": f"2026-11-06 10:00:{start}",
        "duration_ms": 100.0,
        "status_code": kw.pop("status", "Unset"),
        "attrs": kw.pop("attrs", {}),
        "bundle_digest": kw.pop("digest", ""),
        **kw,
    }


def _retrieval(start: str, ids: list[str], scores: list[float], **kw: Any) -> dict[str, Any]:
    return _row("retrieval", start, retrieved_doc_ids=ids, retrieval_scores=scores, **kw)


def test_golden_removed_moves_when_golden_leaves_the_top_k_and_scores_drop() -> None:
    clean_rows = [_retrieval("01.000", ["g1", "d1"], [0.6, 0.4])]
    fault_rows = [_retrieval("01.000", ["d1", "d2"], [0.4, 0.3], attrs={checks.MARKER: ID})]
    clean = checks.features(clean_rows, {"g1"})
    faulted = checks.features(fault_rows, {"g1"})
    out = checks.signal("golden_context_removed", fault_rows, faulted, clean, injection_id=ID)
    assert out["moved"]
    assert out["golden_in_topk"] == [True, False]
    assert out["max_score"] == [0.6, 0.4]


def test_golden_removed_does_not_move_when_golden_stays() -> None:
    rows = [_retrieval("01.000", ["g1"], [0.6])]
    f = checks.features(rows, {"g1"})
    assert not checks.signal("golden_context_removed", rows, f, f, injection_id=ID)["moved"]


@pytest.mark.parametrize(
    ("mode", "clean_hit", "counted"),
    [
        ("remove", True, True),
        ("remove", False, False),
        ("remove", None, False),
        ("distractor", True, True),
        ("distractor", False, True),
        ("distractor", None, True),
    ],
)
def test_a_retrieval_fault_counts_with_golden_in_the_clean_top_k_or_as_a_distractor(
    mode: str, clean_hit: bool | None, counted: bool
) -> None:
    assert checks.retrieval_counted(mode, clean_hit) is counted


def test_golden_removed_records_its_mode_and_whether_it_counts() -> None:
    # The clean run never had the golden chunk in its top-k: a distractor still replaces the
    # results (lower scores), so it counts; the same trace as "remove" would not.
    clean_rows = [_retrieval("01.000", ["d1", "d2"], [0.5, 0.4])]
    fault_rows = [_retrieval("01.000", ["x1", "x2"], [0.1, 0.0], attrs={checks.MARKER: ID})]
    clean = checks.features(clean_rows, {"g1"})
    faulted = checks.features(fault_rows, {"g1"})
    out = checks.signal(
        "golden_context_removed", fault_rows, faulted, clean, injection_id=ID, mode="distractor"
    )
    assert out["moved"]
    assert out["golden_in_topk"] == [False, False]
    assert (out["mode"], out["counted"]) == ("distractor", True)
    out = checks.signal(
        "golden_context_removed", fault_rows, faulted, clean, injection_id=ID, mode="remove"
    )
    assert (out["mode"], out["counted"]) == ("remove", False)


def test_tool_failure_needs_an_error_tool_span_consumed_by_a_later_llm_step() -> None:
    rows = [
        _row("tool", "01.000", status="Error", tool_name="retriever", args_hash="a",
             error_type="ToolFailure", attrs={checks.MARKER: ID}),
        _row("llm", "02.000", attrs={"aegis.output.parse_ok": "true"}),
    ]  # fmt: skip
    out = checks.signal("tool_failure", rows, checks.features(rows), None, injection_id=ID)
    assert out["moved"]
    assert out["error_type"] == ["ToolFailure"]
    assert out["retried"] == 0
    alone = rows[:1]
    assert not checks.signal("tool_failure", alone, checks.features(alone), None, injection_id=ID)[
        "moved"
    ]


def test_tool_failure_counts_a_retry_of_the_same_call() -> None:
    rows = [
        _row("tool", "01.000", status="Error", tool_name="retriever", args_hash="a"),
        _row("llm", "02.000"),
        _row("tool", "03.000", tool_name="retriever", args_hash="a"),
    ]
    assert checks.features(rows).tool_errors_retried == 1


def test_malformed_output_reads_parse_ok_on_the_marked_span() -> None:
    clean_rows = [_row("llm", "01.000", attrs={"aegis.output.parse_ok": "true"})]
    rows = [
        _row("llm", "01.000", attrs={"aegis.output.parse_ok": "false", checks.MARKER: ID}),
        _row("llm", "02.000", attrs={"aegis.output.parse_ok": "true"}),
    ]
    out = checks.signal(
        "malformed_output",
        rows,
        checks.features(rows),
        checks.features(clean_rows),
        injection_id=ID,
    )
    assert out == {"moved": True, "parse_ok": [["true"], ["false"]]}


def test_output_budget_reads_finish_reason_length() -> None:
    rows = [_row("llm", "01.000", finish_reasons=["length"], attrs={checks.MARKER: ID})]
    out = checks.signal(
        "output_budget_exhausted", rows, checks.features(rows), None, injection_id=ID
    )
    assert out["moved"]


def test_step_memory_loss_counts_repeats_of_one_call() -> None:
    rows = [_row("tool", f"0{i}.000", tool_name="retriever", args_hash="a") for i in range(3)]
    f = checks.features(rows)
    assert f.loop_max == 3
    assert checks.signal("step_memory_loss", rows, f, None, injection_id=ID, repeat_min=3)["moved"]
    assert not checks.signal("step_memory_loss", rows, f, None, injection_id=ID, repeat_min=4)[
        "moved"
    ]


def test_prompt_variant_moves_the_digest() -> None:
    base, variant = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    clean = checks.features([_row("agent", "01.000", digest=base)])
    rows = [_row("agent", "01.000", digest=variant, attrs={checks.MARKER: ID})]
    f = checks.features(rows)
    assert checks.signal("prompt_variant", rows, f, clean, injection_id=ID, bundle_digest=variant)[
        "moved"
    ]
    assert not checks.signal("prompt_variant", rows, f, f, injection_id=ID)["moved"]


def test_marker_kind() -> None:
    rows = [_row("retrieval", "01.000", attrs={checks.MARKER: ID}), _row("llm", "02.000")]
    assert checks.marker_kinds_ok(rows, ID, "retriever") == (True, ["retrieval"])
    assert checks.marker_kinds_ok(rows, ID, "llm") == (False, ["retrieval"])
    assert checks.marker_kinds_ok(rows, "other", "llm") == (False, [])


def test_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown"):
        checks.signal("nope", [], checks.features([]), None, injection_id=ID)
