"""tools/faults/progress.py (#70): a run's progress from its files alone. Read-only: no kind, no
lock, nothing written; it reads next to a running attempt and through a torn ledger tail."""

import json
import os
from pathlib import Path

import faultslib  # tools/faults, on pytest's pythonpath as at the driver's run time
import progress
import pytest

from traceblame_faults import appendlog, attempts, ledger, plan, runner, scenario

RUN = "faults-d1-20261117-s19-abc1234"
COMMIT = "abcdef0123456789abcdef0123456789abcdef01"


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(faultslib, "WORK", tmp_path)
    (tmp_path / RUN).mkdir()
    (tmp_path / RUN / "run.json").write_text(
        json.dumps(
            {"run_id": RUN, "seed": 19, "traceblame_commit": COMMIT, "dirty": False,
             "created_at": "2026-11-17T10:00:00Z", "plan": {"name": "d1"}}
        ),
        encoding="utf-8",
    )  # fmt: skip
    return tmp_path


def _entries() -> list[plan.Entry]:
    queries = [
        plan.Query(f"Contract {i}__Law", "agentic-rag", "cuad", "answerable", "mapped")
        for i in range(3)
    ]
    return plan.plan(
        seed=19,
        eval_run_id=RUN,
        app="agentic-rag",
        queries=queries,
        counts={"tool_failure": 3},
        clean=3,
        scenarios=scenario.load_all(),
    )


def _record(entry: plan.Entry, n: int, load: float) -> ledger.Record:
    return ledger.Record(
        trace_id=f"{n:032x}",
        eval_run_id=RUN,
        service="agentic-rag",
        source="cuad",
        query_ref=entry.query_ref,
        stratum="answerable",
        golden_doc_ids=(),
        golden_status="unmapped",
        planned_fault_kind=entry.kind,
        injection_id="",
        fault_kind="",
        target_component="",
        host_load1=load,
    )


def _run(work: Path) -> tuple[Path, list[plan.Entry], runner.Attempt]:
    """Two entries done (one clean, one faulted), a third in flight."""
    d = work / RUN / "agentic-rag"
    d.mkdir()
    entries = _entries()
    (d / "plan.jsonl").write_text(plan.dumps(entries), encoding="utf-8")
    attempt = runner.open_attempt(d, entries, eval_run_id=RUN, provenance={"c": 1})
    log = attempt.entry_log()
    for n, entry in enumerate(entries[:2]):
        log.start(entry)
        log.record(entry, _record(entry, n, 2.0 + n))
        appendlog.append(
            d / f"outcomes.{attempt.id}.jsonl",
            json.dumps({"kind": entry.kind, "seconds": 40.0 + 20 * n, "status": "clean"}),
        )
    log.start(entries[2])
    log.trace(entries[2], f"{7:032x}")
    return d, entries, attempt


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def test_progress_counts_entries_by_kind_and_writes_nothing(work: Path) -> None:
    d, entries, attempt = _run(work)
    before = _snapshot(work)
    p = progress.progress(RUN)
    assert _snapshot(work) == before, "read-only"
    assert (p["plan"], list(p["apps"])) == ("d1", ["agentic-rag"])
    v = p["apps"]["agentic-rag"]
    assert (v["entries"], v["done"], v["remaining"], v["abandoned"]) == (6, 2, 4, 0)
    kinds_done = sorted(e.kind or "clean" for e in entries[:2])
    assert sum(row["done"] for row in v["by_kind"].values()) == 2
    for kind in set(kinds_done):
        assert v["by_kind"][kind]["done"] == kinds_done.count(kind)
    assert v["host_load1_median"] == 2.5
    assert v["torn_tail"] is None
    assert v["last_ledger_line_age_seconds"] >= 0
    attempt.end()
    assert d.is_dir()


def test_a_running_attempt_shows_its_entry_in_flight_and_a_dead_one_is_killed(work: Path) -> None:
    _, entries, attempt = _run(work)
    v = progress.progress(RUN)["apps"]["agentic-rag"]
    (view,) = v["attempts"]
    assert (view["id"], view["status"], view["pid"]) == (attempt.id, "running", os.getpid())
    assert v["current"]["entry_id"] == entries[2].entry_id
    assert v["current"]["trace_ids"] == [f"{7:032x}"]
    assert v["current"]["seconds"] >= 0
    text = progress.render(progress.progress(RUN))
    assert f"attempt {attempt.id}: running (pid {os.getpid()})" in text
    assert f"in flight: {entries[2].entry_id}" in text
    # The same log with a pid that is gone: not resumed yet.
    path = work / RUN / "agentic-rag" / attempts.FILE
    path.write_text(
        path.read_text(encoding="utf-8").replace(f'"pid":{os.getpid()}', '"pid":2147483646'),
        encoding="utf-8",
    )
    v = progress.progress(RUN)["apps"]["agentic-rag"]
    assert v["attempts"][0]["status"] == "killed (not resumed yet)"
    assert v["current"] is None
    os.close(attempt.lock_fd)


def test_the_eta_uses_this_runs_medians_and_falls_back_to_the_validation_runs(
    work: Path,
) -> None:
    _, entries, attempt = _run(work)
    fallback = progress.fallback_medians()["agentic-rag"]
    assert fallback["tool_failure"] > 0, "the committed validation report has the medians"
    v = progress.app_progress(work / RUN / "agentic-rag", fallback, now=0.0)
    assert v is not None
    measured = {e.kind or "clean" for e in entries[:2]}
    expected = 0.0
    for kind, row in v["by_kind"].items():
        median = v["seconds_median"].get(kind, fallback[kind])
        expected += row["remaining"] * median
        assert v["eta_sources"][kind] == ("this run" if kind in measured else "validation run")
    assert v["eta_seconds"] == round(expected, 1) > 0
    assert v["eta_unknown_kinds"] == []
    bare = progress.app_progress(work / RUN / "agentic-rag", {}, now=0.0)
    assert bare is not None
    assert set(bare["eta_unknown_kinds"]) == {"clean", "tool_failure"} - measured
    assert "ETA: about" in progress.render(progress.progress(RUN))
    attempt.end()


def test_progress_reads_through_a_torn_ledger_tail_and_reports_it(work: Path) -> None:
    d, _, attempt = _run(work)
    with (d / runner.LEDGER).open("ab") as f:
        f.write(b'{"trace_id": "4bf9')
    p = progress.progress(RUN)
    v = p["apps"]["agentic-rag"]
    assert v["done"] == 2
    assert v["torn_tail"]["length"] == len(b'{"trace_id": "4bf9')
    assert "TORN LEDGER TAIL: 18 bytes" in progress.render(p)
    attempt.end()


def test_abandoned_traces_are_counted_by_kind(work: Path) -> None:
    d, entries, attempt = _run(work)
    os.close(attempt.lock_fd)  # killed
    second = runner.open_attempt(d, entries, eval_run_id=RUN, provenance={"c": 1})
    v = progress.progress(RUN)["apps"]["agentic-rag"]
    kind = entries[2].kind or "clean"
    assert v["abandoned"] == 1
    assert v["by_kind"][kind]["abandoned"] == 1
    assert v["abandoned_by_reason"] == {"killed": 1}
    assert [a["status"] for a in v["attempts"]] == ["killed", "running"]
    second.end()
    assert progress.progress(RUN)["apps"]["agentic-rag"]["attempts"][1]["status"] == "completed"


def test_a_run_with_nothing_prepared_and_an_unknown_run(work: Path) -> None:
    p = progress.progress(RUN)
    assert p["apps"] == {}
    assert "nothing prepared yet" in progress.render(p)
    with pytest.raises(faultslib.RunError, match=r"no run\.json"):
        progress.progress("faults-nope")
    assert progress.fallback_medians(work / "no-report.json") == {}
