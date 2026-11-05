"""traceblame-faults: scenarios check, plan, check-ledger, digest."""

import json
from pathlib import Path

import pytest

from traceblame_faults import bundle, cli, ledger, plan


def _queries(tmp: Path) -> Path:
    qs = [
        plan.Query(
            ref=f"q{i}",
            app="agentic-rag",
            source="cuad",
            stratum="answerable",
            golden_status="mapped",
        )
        for i in range(3)
    ]
    path = tmp / "queries.jsonl"
    path.write_text(plan.dump_queries(qs), encoding="utf-8")
    return path


def test_scenarios_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["scenarios", "check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["scenarios"]) == 8


def test_plan_writes_a_reproducible_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "plan",
        "--app",
        "agentic-rag",
        "--queries",
        str(_queries(tmp_path)),
        "--seed",
        "7",
        "--eval-run-id",
        "faults-x",
        "--count",
        "tool_failure=2",
        "--count",
        "malformed_output=1",
        "--clean",
        "1",
    ]
    assert cli.main([*argv, "--out", str(tmp_path / "a.jsonl")]) == 0
    assert cli.main([*argv, "--out", str(tmp_path / "b.jsonl")]) == 0
    a = (tmp_path / "a.jsonl").read_bytes()
    assert a == (tmp_path / "b.jsonl").read_bytes()
    assert len(plan.loads(a.decode())) == 4
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert summary["entries"] == 4
    assert summary["sha256"]


def test_plan_refuses_a_bad_count(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(
            [
                "plan",
                "--app",
                "agentic-rag",
                "--queries",
                str(_queries(tmp_path)),
                "--seed",
                "7",
                "--eval-run-id",
                "x",
                "--count",
                "tool_failure",
                "--out",
                str(tmp_path / "p.jsonl"),
            ]
        )


def test_check_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rec = ledger.Record(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        eval_run_id="faults-x",
        service="agentic-rag",
        source="cuad",
        query_ref="q0",
        stratum="answerable",
        golden_doc_ids=(),
        golden_status="unmapped",
        planned_fault_kind="",
        injection_id="",
        fault_kind="",
        target_component="",
        params={},
        seed=7,
        injected_at=None,
        bundle_digest=None,
    )
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, rec)
    assert cli.main(["check-ledger", "--ledger", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "records": 1,
        "clean": 1,
        "applied": 0,
        "not_reached": 0,
    }


def test_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    system = tmp_path / "system.txt"
    system.write_text("You triage alerts.", encoding="utf-8")
    assert cli.main(["digest", "--app", "runbook-agent", "--template", f"system={system}"]) == 0
    assert capsys.readouterr().out.strip() == bundle.digest(
        "runbook-agent", {"system": "You triage alerts."}
    )
