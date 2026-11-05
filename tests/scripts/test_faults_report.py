"""tools/faults/report.py: what the validation report re-derives from the run's ledger and plan
(modes, counts, host load), the D1 rule for golden_context_removed, and the content-check line.
Ledgers, plans and check.json entries here are test doubles, never eval data."""

from pathlib import Path
from typing import Any

import report  # tools/faults, on pytest's pythonpath as at the driver's run time

from traceblame_faults import ids, ledger, plan

RUN = "faults-20261106-s7-abc1234"
DIGEST = "sha256:" + "a" * 64
KIND = "golden_context_removed"
SCENARIO = "golden_context_removed-agentic-rag"
COMMON: dict[str, Any] = {
    "scenario": SCENARIO,
    "scenario_version": 1,
    "faults_version": "0.1.0",
    "top_k": 5,
}


def _trace_id(n: int) -> str:
    return f"{n:032x}"


def _record(
    n: int, ref: str, kind: str, applied: dict[str, Any] | None, load: float | None
) -> ledger.Record:
    common: dict[str, Any] = {
        "trace_id": _trace_id(n),
        "eval_run_id": RUN,
        "service": "agentic-rag",
        "source": "cuad",
        "query_ref": ref,
        "stratum": "answerable",
        "golden_doc_ids": ("cuad-0123456789ab-3",),
        "golden_status": "mapped",
        "planned_fault_kind": kind,
        "seed": 7,
        "bundle_digest": DIGEST,
        "host_load1": load,
    }
    if applied is None:
        return ledger.Record(
            **common, injection_id="", fault_kind="", target_component="", params={}
        )
    return ledger.Record(
        **common,
        injection_id=ids.injection_id(RUN, kind, ref),
        fault_kind=kind,
        target_component="retrieval",
        params=COMMON | applied | {"baseline_digest": DIGEST},
        injected_at="2026-11-06T10:00:00.123Z",
    )


def _run_dir(tmp_path: Path) -> Path:
    records = [
        _record(0, "Contract A__Law", "", None, 3.5),
        _record(
            1, "Contract A__Law", KIND, {"mode": "distractor", "distractor_doc": "cuad-x1"}, 7.25
        ),
        _record(2, "Contract B__Term", KIND, {"mode": "remove", "removed_n": 2}, 9.0),
        _record(3, "Contract C__Cap", KIND, None, None),  # remove, nothing to remove
    ]
    for r in records:
        ledger.append(tmp_path / "ledger.jsonl", r)
    entries = [
        plan.Entry(r.query_ref, "agentic-rag", KIND, SCENARIO, COMMON | {"mode": m})
        for r, m in zip(records[1:], ("distractor", "remove", "remove"), strict=True)
    ]
    (tmp_path / "plan.jsonl").write_text(plan.dumps(entries), encoding="utf-8")
    return tmp_path


def _check() -> dict[str, Any]:
    def t(n: int, kind: str, status: str, clean_hit: bool | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"trace_id": _trace_id(n), "query": f"q{n}", "kind": kind}
        out["status"] = status
        if status == "applied":
            out["signal"] = {
                "moved": True,
                "golden_in_topk": [clean_hit, False],
                "max_score": [0.2, -0.2],
            }
        return out

    return {
        "per_trace": [
            t(0, "clean", "clean"),
            t(1, KIND, "applied", clean_hit=False),
            t(2, KIND, "applied", clean_hit=True),
            t(3, KIND, "not_reached"),
        ],
        "per_kind": {
            "clean": {"applied": 0, "not_reached": 0},
            KIND: {"applied": 2, "not_reached": 1},
        },
    }


def test_the_ledger_view_rederives_modes_loads_and_counts(tmp_path: Path) -> None:
    view = report.ledger_view(_run_dir(tmp_path))
    assert view["records"] == 4
    assert view["modes"] == {
        _trace_id(0): None,
        _trace_id(1): "distractor",
        _trace_id(2): "remove",
        _trace_id(3): "remove",  # not reached: the planned mode
    }
    assert view["host_load1"][_trace_id(1)] == 7.25
    assert view["host_load1"][_trace_id(3)] is None
    assert view["counts"] == {
        "clean": {"clean": 1, "applied": 0, "not_reached": 0},
        KIND: {"clean": 0, "applied": 2, "not_reached": 1},
    }
    assert report.ledger_problems(_check(), view) == []
    wrong = _check()
    wrong["per_kind"][KIND]["applied"] = 3
    assert report.ledger_problems(wrong, view) == [
        f"{KIND}: check.json and the ledger disagree on applied / not_reached"
    ]


def test_retrieval_rows_apply_the_d1_rule(tmp_path: Path) -> None:
    view = report.ledger_view(_run_dir(tmp_path))
    rows = report.retrieval_rows(_check(), view["modes"])
    assert [(r["query"], r["status"], r["mode"], r["counted"]) for r in rows] == [
        ("q1", "applied", "distractor", True),  # no golden in the clean top-k, but a distractor
        ("q2", "applied", "remove", True),  # golden was in the clean top-k
        ("q3", "not_reached", "remove", False),
    ]
    assert report.loads_by_kind(_check(), view) == {"clean": 3.5, KIND: 9.0}


def test_the_content_line_names_needles_and_everything_scanned() -> None:
    cc = {
        "needles": {"queries": 4, "contract_chunks": 120, "windows": 9000},
        "scanned": {
            "rows": {"spans": 366, "eval_traces": 28},
            "rows_total": 394,
            "exported_spans": 366,
            "files": {"otlp.json": 1, "ledger.jsonl": 28},
            "id_fields_excluded": {"fields": ["query_ref", "ref"], "values": 56},
        },
        "leaking": 0,
    }
    line = report.content_line("agentic-rag", cc)
    assert line.startswith("content check: 0 leaking. Needles: 4 sampled CUAD questions")
    assert "394 rows (eval_traces 28, spans 366)" in line
    assert "the 366 exported spans" in line
    assert "ledger.jsonl (28 lines)" in line
    assert "otlp.json (1 line)" in line
    assert "left out: only the dataset id fields query_ref, ref (56 values" in line
    assert report.content_line("agentic-rag", {"checked": 4, "leaking": 0}).startswith(
        "content check (spans only)"
    )


def test_the_report_keeps_the_run_date() -> None:
    states = {
        "agentic-rag": {"prepared_at": "2026-11-05T15:00:00Z"},
        "runbook-agent": {"built_at": "2026-11-06T01:00:00Z"},
    }
    assert report.run_date(states) == "2026-11-05"


def test_the_markdown_states_the_rule_and_the_empty_blockers(tmp_path: Path) -> None:
    view = report.ledger_view(_run_dir(tmp_path))
    md = report.markdown(
        {
            "date": "2026-11-06",
            "apps": {},
            "retrieval": report.retrieval_rows(_check(), view["modes"]),
            "loop_gate": report.loop_gate(
                {"per_kind": {}}, {"loop": {"hit_rate_min": 0.6, "repeat_min": 3}}
            ),
            "projection": report.projection({}),
            "d1_components": report.components(),
            "blockers": [],
            "problems": [],
        }
    )
    assert "| q1 | distractor | False | False | 0.2 -> -0.2 | True | True |" in md
    assert "| q3 | remove | None | None | None -> None | None | False |" in md
    assert "In this run: 2 applied ({'distractor': 1, 'remove': 1}), 2 counted" in md
    assert "None recorded." in md
