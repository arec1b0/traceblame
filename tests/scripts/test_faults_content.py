"""tools/faults/faultslib.py: the content check covers every stored row of the run in every
table and the run's files, and a re-check replaces only its own result (ClickHouse is a test
double here; the rows are never eval data)."""

import json
from pathlib import Path
from typing import Any

import faultslib  # tools/faults, on pytest's pythonpath as at the driver's run time
import pytest

from traceblame_faults import ledger

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
    assert seen["files"] == ["otlp.json.json", "ledger.jsonl.json"]
    ledger_copy = json.loads((d / "content-scan" / "ledger.jsonl.json").read_text("utf-8"))
    assert "query_ref" not in ledger_copy[0]
    assert ledger_copy[0]["trace_id"] == TRACE
    assert check["content_check"]["scanned"] == {
        "rows": {"spans": 3, "eval_traces": 1},
        "rows_total": 4,
        "exported_spans": 1,
        "files": {"otlp.json": 1, "ledger.jsonl": 1},
        "id_fields_excluded": {"fields": ["query_ref", "ref"], "values": 2},
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


def test_strip_ids_drops_only_the_dataset_id_fields_at_any_depth() -> None:
    value = {"query_ref": "a", "x": [{"ref": "b", "y": "c"}], "attrs": {"query": "kept"}}
    assert faultslib.strip_ids(value) == ({"x": [{"y": "c"}], "attrs": {"query": "kept"}}, 2)
