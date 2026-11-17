"""The resumable loop (#70), in one process: attempts, recovery of entries in flight, the lock,
the provenance check. The kill itself is tests/scripts/test_faults_resume.py."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from traceblame_faults import attempts, ids, ledger, plan, runner, scenario

RUN = "faults-20261117-s19-abc1234"
PROVENANCE = {"commit": "abc1234", "plan_sha256": "1" * 64, "images": {"gateway": ["sha256:aa"]}}


def _entries() -> list[plan.Entry]:
    queries = [
        plan.Query(f"Contract {i}__Governing Law", "agentic-rag", "cuad", "answerable", "mapped")
        for i in range(3)
    ]
    return plan.plan(
        seed=19,
        eval_run_id=RUN,
        app="agentic-rag",
        queries=queries,
        counts={"tool_failure": 2},
        clean=3,
        scenarios=scenario.load_all(),
    )


def _trace(n: int) -> str:
    return f"{n:032x}"


def _record(entry: plan.Entry, trace_id: str) -> ledger.Record:
    return ledger.Record(
        trace_id=trace_id,
        eval_run_id=RUN,
        service=entry.app,
        source="cuad",
        query_ref=entry.query_ref,
        stratum="answerable",
        golden_doc_ids=(),
        golden_status="unmapped",
        planned_fault_kind=entry.kind,
        injection_id="",
        fault_kind="",
        target_component="",
    )


def _open(run_dir: Path, **kwargs: object) -> runner.Attempt:
    return runner.open_attempt(
        run_dir,
        _entries(),
        eval_run_id=RUN,
        provenance=PROVENANCE,
        **kwargs,  # type: ignore[arg-type]
    )


def _events(run_dir: Path, name: str | None = None) -> list[dict[str, object]]:
    events = attempts.read(run_dir / attempts.FILE)
    return [e for e in events if name is None or e["event"] == name]


def test_a_first_attempt_runs_every_entry_in_plan_order_and_ends(tmp_path: Path) -> None:
    entries = _entries()
    attempt = _open(tmp_path)
    assert attempt.id.startswith("a001-")
    assert attempt.remaining == entries
    ran: list[str] = []

    def run_one(entry: plan.Entry, log: runner.EntryLog) -> ledger.Record:
        ran.append(entry.entry_id)
        log.trace(entry, _trace(len(ran)))
        return _record(entry, _trace(len(ran)))

    assert runner.run_entries(attempt.entry_log(), entries, run_one) == len(entries)
    attempt.end()
    assert ran == [e.entry_id for e in entries]
    records = ledger.read(tmp_path / runner.LEDGER)
    assert [r.entry_id for r in records] == ran
    assert {r.attempt_id for r in records} == {attempt.id}
    (view,) = attempts.attempts(_events(tmp_path))
    assert (view.status, view.pid) == ("completed", os.getpid())


def test_a_second_attempt_after_a_clean_finish_has_nothing_to_run(tmp_path: Path) -> None:
    first = _open(tmp_path)
    runner.run_entries(
        first.entry_log(), _entries(), lambda e, _: _record(e, _trace(hash(e.entry_id) % 10**9))
    )
    first.end()
    second = _open(tmp_path)
    assert second.id.startswith("a002-")
    assert second.remaining == []
    assert second.recovery == runner.Recovery()
    assert runner.run_entries(second.entry_log(), _entries(), lambda e, _: None) == 0
    second.end()


def test_an_attempt_without_an_end_is_killed_and_its_entry_in_flight_abandoned(
    tmp_path: Path,
) -> None:
    entries = _entries()
    first = _open(tmp_path)
    log = first.entry_log()
    log.start(entries[0])
    log.trace(entries[0], _trace(1))
    log.record(entries[0], _record(entries[0], _trace(1)))
    log.start(entries[1])
    log.trace(entries[1], _trace(2))
    os.close(first.lock_fd)  # the process is gone: no attempt_end

    second = _open(tmp_path)
    assert second.recovery.killed == (first.id,)
    (killed,) = _events(tmp_path, "attempt_killed")
    assert killed["status"] == "killed/unknown"
    assert killed["last_event"] == f"entry_trace {entries[1].entry_id}"
    (gone,) = second.recovery.abandoned
    assert gone == attempts.Abandoned(
        _trace(2), RUN, "agentic-rag", entries[1].entry_id, first.id,
        entries[1].kind, entries[1].injection_id, "killed", "attempt_log",
    )  # fmt: skip
    (lost,) = second.recovery.lost
    assert (lost["entry_id"], lost["reason"], lost["trace_ids"]) == (
        entries[1].entry_id, "killed", [_trace(2)],
    )  # fmt: skip
    assert second.remaining == entries[1:]
    # The entry runs again under a new trace id; the abandoned one never reaches the ledger.
    runner.run_entries(
        second.entry_log(), entries, lambda e, _: _record(e, _trace(100 + entries.index(e)))
    )
    second.end()
    records = ledger.read(tmp_path / runner.LEDGER)
    assert sorted(r.entry_id or "" for r in records) == sorted(e.entry_id for e in entries)
    assert _trace(2) not in {r.trace_id for r in records}
    # A third attempt does not record the same loss again.
    third = _open(tmp_path)
    assert third.recovery == runner.Recovery()
    third.end()
    assert len(_events(tmp_path, "entry_lost")) == 1


def test_an_entry_in_flight_without_a_known_trace_id_is_lost_with_no_trace(tmp_path: Path) -> None:
    entries = _entries()
    first = _open(tmp_path)
    first.entry_log().start(entries[0])
    os.close(first.lock_fd)
    second = _open(tmp_path)
    assert second.recovery.abandoned == ()
    (lost,) = second.recovery.lost
    assert lost["trace_ids"] == []
    assert second.remaining == entries
    second.end()


def test_salvage_adds_trace_ids_found_outside_the_attempt_log(tmp_path: Path) -> None:
    entries = _entries()
    first = _open(tmp_path)
    first.entry_log().start(entries[0])
    os.close(first.lock_fd)
    asked: list[str] = []

    def salvage(pending: object) -> dict[str, list[str]]:
        asked.extend(s.entry_id for s in pending)  # type: ignore[attr-defined]
        return {entries[0].entry_id: [_trace(7)]}

    second = _open(tmp_path, salvage=salvage)
    assert asked == [entries[0].entry_id]
    (gone,) = second.recovery.abandoned
    assert (gone.trace_id, gone.source, gone.reason) == (_trace(7), "preload", "killed")
    second.end()


def test_a_kill_between_the_record_and_the_next_entry_start_loses_nothing(tmp_path: Path) -> None:
    entries = _entries()
    first = _open(tmp_path)
    log = first.entry_log()
    log.start(entries[0])
    log.record(entries[0], _record(entries[0], _trace(1)))
    os.close(first.lock_fd)
    second = _open(tmp_path)
    assert second.recovery.killed == (first.id,)
    assert (second.recovery.lost, second.recovery.abandoned) == ((), ())
    assert second.remaining == entries[1:]
    second.end()


def test_a_kill_before_the_first_entry_leaves_every_entry_to_run(tmp_path: Path) -> None:
    first = _open(tmp_path)
    os.close(first.lock_fd)
    second = _open(tmp_path)
    assert second.recovery.killed == (first.id,)
    assert second.remaining == _entries()
    second.end()


@pytest.mark.parametrize("whole", [False, True])
def test_a_torn_ledger_tail_is_copied_aside_and_its_entry_runs_again(
    tmp_path: Path, whole: bool
) -> None:
    entries = _entries()
    first = _open(tmp_path)
    log = first.entry_log()
    log.start(entries[0])
    log.record(entries[0], _record(entries[0], _trace(1)))
    log.start(entries[1])
    log.trace(entries[1], _trace(2))
    line = ledger.dumps(
        replace(_record(entries[1], _trace(2)), entry_id=entries[1].entry_id, attempt_id=first.id)
    ).encode()
    tail = line if whole else line[: len(line) // 2]  # whole JSON without its newline, or half
    with (tmp_path / runner.LEDGER).open("ab") as f:
        f.write(tail)
    os.close(first.lock_fd)
    with pytest.raises(ledger.LedgerError, match="torn tail"):
        ledger.read(tmp_path / runner.LEDGER)

    second = _open(tmp_path)
    assert second.recovery.torn is not None
    assert second.recovery.torn.length == len(tail)
    (event,) = _events(tmp_path, "torn_tail")
    assert (tmp_path / str(event["copy"])).read_bytes() == tail
    (gone,) = second.recovery.abandoned
    assert (gone.trace_id, gone.entry_id, gone.reason) == (
        _trace(2), entries[1].entry_id, "torn_tail",
    )  # fmt: skip
    assert [r.trace_id for r in ledger.read(tmp_path / runner.LEDGER)] == [_trace(1)]
    assert second.remaining == entries[1:]
    second.end()


def test_a_failed_attempt_abandons_its_entry_in_flight_as_failed(tmp_path: Path) -> None:
    entries = _entries()
    first = _open(tmp_path)
    log = first.entry_log()
    log.start(entries[0])
    log.trace(entries[0], _trace(1))
    first.end("failed: RuntimeError")
    second = _open(tmp_path)
    assert second.recovery.killed == ()
    (gone,) = second.recovery.abandoned
    assert gone.reason == "failed"
    second.end()


def test_an_entry_that_ends_without_a_record_abandons_its_traces_and_runs_again(
    tmp_path: Path,
) -> None:
    entries = _entries()
    first = _open(tmp_path)

    def run_one(entry: plan.Entry, log: runner.EntryLog) -> ledger.Record | None:
        if entry is entries[0]:
            log.trace(entry, _trace(1))
            return None
        log.trace(entry, _trace(50))  # a first trace the record does not name
        return _record(entry, _trace(10 + entries.index(entry)))

    runner.run_entries(first.entry_log(), entries[:2], run_one)
    first.end()
    gone = {a.trace_id: a.reason for a in attempts.abandoned(_events(tmp_path))}
    assert gone == {_trace(1): "no_record", _trace(50): "no_record"}
    second = _open(tmp_path)
    assert (second.recovery.lost, second.recovery.abandoned) == ((), ())
    assert second.remaining == [entries[0], *entries[2:]]
    second.end()


def test_a_second_attempt_is_refused_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    first = _open(tmp_path)
    with pytest.raises(runner.BusyError, match=rf"attempt {first.id} still running, pid \d+"):
        _open(tmp_path)
    first.end()
    _open(tmp_path).end()


def test_the_lock_is_held_while_a_child_that_inherited_it_lives(tmp_path: Path) -> None:
    first = _open(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        pass_fds=(first.lock_fd,),
    )
    try:
        os.close(first.lock_fd)  # the driver is gone; its child still holds the descriptor
        with pytest.raises(runner.BusyError, match="still running"):
            _open(tmp_path)
    finally:
        child.communicate(b"")
    second = _open(tmp_path)
    assert second.recovery.killed == (first.id,)
    second.end()


def test_a_changed_provenance_is_refused_by_name_and_recorded(tmp_path: Path) -> None:
    _open(tmp_path).end()
    changed = PROVENANCE | {"images": {"gateway": ["sha256:bb"]}, "model": "qwen"}
    with pytest.raises(runner.ProvenanceError, match=r"images\.gateway") as info:
        runner.open_attempt(tmp_path, _entries(), eval_run_id=RUN, provenance=changed)
    assert "model: absent" in str(info.value)
    assert "new run id" in str(info.value)
    (refused,) = _events(tmp_path, "resume_refused")
    assert len(refused["differences"]) == 2  # type: ignore[arg-type]
    assert len(_events(tmp_path, "attempt_start")) == 1
    _open(tmp_path).end()  # the first provenance still resumes


def test_differences_name_every_changed_added_and_removed_key() -> None:
    a = {"commit": "1", "images": {"x": ["a"], "y": ["b"]}, "gone": 1}
    b = {"commit": "1", "images": {"x": ["a"], "y": ["c"]}, "new": 2}
    assert runner.differences(a, b) == [
        "gone: 1 -> absent",
        'images.y: ["b"] -> ["c"]',
        "new: absent -> 2",
    ]
    assert runner.differences(a, a) == []
    assert runner.digest(a) != runner.digest(b)
    assert runner.digest(a) == runner.digest(dict(reversed(list(a.items()))))


def test_a_ledger_without_a_first_provenance_is_not_resumable(tmp_path: Path) -> None:
    ledger.append(tmp_path / runner.LEDGER, _record(_entries()[0], _trace(1)))
    with pytest.raises(runner.RunnerError, match="not a resumable run"):
        _open(tmp_path)


def test_a_ledger_entry_the_plan_does_not_have_is_refused(tmp_path: Path) -> None:
    first = _open(tmp_path)
    other = plan.Entry(
        "Other__X", "agentic-rag", "", "", {}, "", 19, "agentic-rag:clean:" + "0" * 12
    )
    other = replace(other, entry_id=ids.entry_id("agentic-rag", "", "Other__X"))
    first.entry_log().record(other, _record(other, _trace(1)))
    first.end()
    with pytest.raises(runner.RunnerError, match="the plan does not"):
        _open(tmp_path)


def test_a_record_of_another_run_and_a_bad_trace_id_are_refused(tmp_path: Path) -> None:
    entries = _entries()
    attempt = _open(tmp_path)
    log = attempt.entry_log()
    with pytest.raises(runner.RunnerError, match="trace id"):
        log.trace(entries[0], "nope")
    with pytest.raises(runner.RunnerError, match="a record of run"):
        log.record(entries[0], replace(_record(entries[0], _trace(1)), eval_run_id="other-run"))
    attempt.end()
    attempt.end()  # idempotent


def test_a_dead_attempts_helper_is_stopped_on_resume_and_a_reused_pid_is_left_alone(
    tmp_path: Path,
) -> None:
    first = _open(tmp_path)
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        first.helper("collector", helper.pid)
        # A helper event whose pid now belongs to another process (another start time).
        attempts.Log(tmp_path / attempts.FILE).append(
            "helper", attempt_id=first.id, name="port-forward", pid=bystander.pid, started=1
        )
        os.close(first.lock_fd)
        second = _open(tmp_path)
        assert [h["name"] for h in second.recovery.helpers_stopped] == ["collector"]
        assert helper.wait(timeout=10) != 0
        assert bystander.poll() is None
        second.end()
    finally:
        for proc in (helper, bystander):
            proc.kill()
            proc.wait()


def test_notes_are_kept_per_attempt(tmp_path: Path) -> None:
    attempt = _open(tmp_path, nats_seq_before=41)
    attempt.note(spans_sent=3)
    attempt.note(seconds=1.5)
    doc = json.loads((tmp_path / f"attempt.{attempt.id}.json").read_text(encoding="utf-8"))
    assert doc == {"spans_sent": 3, "seconds": 1.5}
    (start,) = _events(tmp_path, "attempt_start")
    assert (start["nats_seq_before"], start["remaining"]) == (41, len(_entries()))
    attempt.end()
