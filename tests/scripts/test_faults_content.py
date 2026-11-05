"""tools/faults/faultslib.py: the content check covers every stored row of the run in every
table and the run's files, and a re-check replaces only its own result (ClickHouse is a test
double here; the rows are never eval data)."""

import json
from pathlib import Path
from typing import Any

import faultslib  # tools/faults, on pytest's pythonpath as at the driver's run time
import pytest

from traceblame_faults import ledger, plan

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CLEAN = ledger.Record(
    trace_id=TRACE,
    eval_run_id="faults-20261106-s7-abc1234",
    service="runbook-agent",
    source="runbook",
    query_ref="oom-1@s7",
    stratum="oom",
    golden_doc_ids=(),
    golden_status="none",
    planned_fault_kind="",
    injection_id="",
    fault_kind="",
    target_component="",
)
OTLP = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": TRACE, "spanId": "1"}]}]}]}


def test_json_docs_reads_json_json_lines_and_keeps_other_lines(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"k": "line\\nbreak"}', encoding="utf-8")
    (tmp_path / "b.jsonl").write_text('{"n": 1}\n\nnot json\n{"n": 2}\n', encoding="utf-8")
    assert faultslib.json_docs(tmp_path / "a.json") == [{"k": "line\nbreak"}]
    assert faultslib.json_docs(tmp_path / "b.jsonl") == [{"n": 1}, "", "not json", {"n": 2}]


def _run(tmp_path: Path) -> Path:
    ledger.append(tmp_path / "ledger.jsonl", CLEAN)
    entry = plan.Entry("oom-1@s7", "runbook-agent", "", "")
    (tmp_path / "plan.jsonl").write_text(plan.dumps([entry]), encoding="utf-8")
    (tmp_path / "otlp.json").write_text(json.dumps(OTLP), encoding="utf-8")
    (tmp_path / "needles.json").write_text('["the needle"]', encoding="utf-8")
    check = {"problems": ["other", "content found: old"], "content_check": {"checked": 1}}
    (tmp_path / "check.json").write_text(json.dumps(check), encoding="utf-8")
    return tmp_path


def test_recheck_scans_every_table_and_the_run_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _run(tmp_path)
    tables = {
        "spans": [{"trace_id": TRACE, "attrs": {"k": "v"}}] * 3,
        "eval_traces": [{"trace_id": TRACE, "query_ref": "oom-1@s7"}],
    }
    asked: list[set[str]] = []

    def run_rows(ids: Any) -> dict[str, list[dict[str, Any]]]:
        asked.append(set(ids))
        return tables

    monkeypatch.setattr(faultslib, "run_rows", run_rows)
    seen: dict[str, Any] = {}

    def content(got: dict[str, list[dict[str, Any]]], files: list[Path]) -> dict[str, Any]:
        seen["tables"], seen["files"] = got, [f.name for f in files]
        return {"needles": {"n": 1}, "leaking": 0}

    check = faultslib.recheck_content(d, content, lambda _: None)
    assert asked == [{TRACE}]
    # Every row and column, without the dataset id fields.
    assert seen["tables"] == {
        "spans": [{"trace_id": TRACE, "attrs": {"k": "v"}}] * 3,
        "eval_traces": [{"trace_id": TRACE}],
    }
    # Parsed copies of the run's own files, never the needles file.
    assert seen["files"] == ["otlp.json.json", "ledger.jsonl.json", "plan.jsonl.json"]
    ledger_copy = json.loads((d / "content-scan" / "ledger.jsonl.json").read_text("utf-8"))
    assert "query_ref" not in ledger_copy[0]
    assert ledger_copy[0]["trace_id"] == TRACE
    assert check["content_check"]["scanned"] == {
        "rows": {"spans": 3, "eval_traces": 1},
        "rows_total": 4,
        "exported_spans": 1,
        "files": {"otlp.json": 1, "ledger.jsonl": 1, "plan.jsonl": 1},
        "id_fields_excluded": {"fields": ["query_ref", "ref"], "values": 3},
    }
    assert check["problems"] == ["other"]
    assert json.loads((d / "check.json").read_text(encoding="utf-8")) == check


def test_a_leak_found_by_the_recheck_is_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _run(tmp_path)

    def run_rows(_ids: Any) -> dict[str, list[dict[str, Any]]]:
        return {"spans": []}

    monkeypatch.setattr(faultslib, "run_rows", run_rows)
    check = faultslib.recheck_content(
        d, lambda _t, _f: {"needles": {}, "leaking": 2}, lambda _: None
    )
    assert check["problems"][0] == "other"
    assert check["problems"][1].startswith("content found:")


IDS = frozenset({"oom-1@s7", "Contract A_EX-10.1__Governing Law"})


def test_strip_ids_drops_only_exact_dataset_ids_of_the_run_at_any_depth() -> None:
    value = {
        "query_ref": "oom-1@s7",
        "x": [{"ref": "Contract A_EX-10.1__Governing Law", "y": "c"}],
        "attrs": {"query": "kept"},
    }
    assert faultslib.strip_ids(value, IDS) == (
        {"x": [{"y": "c"}], "attrs": {"query": "kept"}},
        2,
    )


@pytest.mark.parametrize(
    "value",
    [
        "Highlight the parts of this contract related to the term",  # not an id: text
        "oom-1@s7 and the alert summary",  # an id with text around it
        "oom-9@s7",  # id-shaped, not one of the run's ids
        "Contract B__Governing Law",  # CUAD-shaped, not one of the run's ids
        "oom-1@s7\n",
        "",
    ],
)
def test_any_other_value_in_an_id_field_is_scanned(value: str) -> None:
    row = {"query_ref": value, "ref": value}
    assert faultslib.strip_ids(row, IDS) == (row, 0)


def test_a_non_id_string_in_query_ref_is_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _run(tmp_path)
    needle = "the alert summary text"

    def run_rows(_ids: Any) -> dict[str, list[dict[str, Any]]]:
        return {"eval_traces": [{"trace_id": TRACE, "query_ref": f"oom-1@s7 {needle}"}]}

    monkeypatch.setattr(faultslib, "run_rows", run_rows)

    def content(tables: dict[str, list[dict[str, Any]]], files: list[Path]) -> dict[str, Any]:
        hits = sum(needle in json.dumps(r) for rows in tables.values() for r in rows)
        hits += sum(needle in f.read_text(encoding="utf-8") for f in files)
        return {"needles": {"n": 1}, "leaking": hits}

    check = faultslib.recheck_content(d, content, lambda _: None)
    assert check["content_check"]["leaking"] == 1
    assert check["problems"][-1].startswith("content found:")


def test_the_run_ids_are_the_queries_and_plan_refs(tmp_path: Path) -> None:
    d = _run(tmp_path)
    (d / "queries.jsonl").write_text('{"ref": "oom-2@s7"}\n', encoding="utf-8")
    assert faultslib.run_ids(d) == frozenset({"oom-1@s7", "oom-2@s7"})
