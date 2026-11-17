"""The attempt log (#70): append-only events, and the views a resume reads from them."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from traceblame_faults import attempts

A1, A2 = "a001-20261117T100000Z", "a002-20261117T110000Z"
ENTRY = "agentic-rag:tool_failure:0123456789ab"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
GONE = attempts.Abandoned(
    TRACE, "faults-d1-x", "agentic-rag", ENTRY, A1, "tool_failure",
    "faults-d1-x:tool_failure:0123456789ab", "killed", "attempt_log",
)  # fmt: skip


def _log(tmp_path: Path) -> attempts.Log:
    return attempts.Log(tmp_path / attempts.FILE)


def test_events_are_appended_with_their_time_and_read_back_in_order(tmp_path: Path) -> None:
    log = _log(tmp_path)
    first = log.append("attempt_start", attempt_id=A1, pid=41, provenance_digest="sha256:aa")
    log.append("entry_start", attempt_id=A1, entry_id=ENTRY)
    events = attempts.read(log.path)
    assert [e["event"] for e in events] == ["attempt_start", "entry_start"]
    assert events[0] == first
    assert attempts.seconds(first["at"]) == pytest.approx(datetime.now(UTC).timestamp(), abs=60)
    assert attempts.read(tmp_path / "missing.jsonl") == []


def test_a_stamp_is_utc_with_milliseconds_and_converts_back() -> None:
    at = attempts.stamp(datetime(2026, 11, 17, 10, 0, 0, 123456, tzinfo=UTC))
    assert at == "2026-11-17T10:00:00.123Z"
    assert attempts.seconds(at) == datetime(2026, 11, 17, 10, 0, 0, 123000, tzinfo=UTC).timestamp()


def test_unknown_events_malformed_lines_and_torn_tails_are_refused(tmp_path: Path) -> None:
    log = _log(tmp_path)
    with pytest.raises(attempts.AttemptsError, match="unknown event"):
        log.append("entry_done", attempt_id=A1)
    log.append("attempt_start", attempt_id=A1, pid=41)
    good = log.path.read_text(encoding="utf-8")
    for bad, match in (
        ("not json\n", "line 2"),
        ('["a list"]\n', "not an attempt event"),
        ('{"event":"entry_done","at":"x"}\n', "not an attempt event"),
        ('{"event":"entry_start"}\n', "not an attempt event"),
        ('{"event":"entry_start","at"', "torn tail"),
    ):
        log.path.write_text(good + bad, encoding="utf-8")
        with pytest.raises(attempts.AttemptsError, match=match):
            attempts.read(log.path)
    with pytest.raises(attempts.AttemptsError, match="torn tail"):
        log.append("entry_start", attempt_id=A1, entry_id=ENTRY)


def test_attempts_are_open_completed_failed_or_killed(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append("attempt_start", attempt_id=A1, pid=41, provenance_digest="sha256:aa")
    log.append("helper", attempt_id=A1, name="collector", pid=42, started=7)
    log.append("entry_start", attempt_id=A1, entry_id=ENTRY)
    (one,) = attempts.attempts(attempts.read(log.path))
    assert (one.status, one.pid, one.last_event) == ("open", 41, f"entry_start {ENTRY}")
    log.append("attempt_killed", attempt_id=A1, status="killed/unknown")
    log.append("entry_start", attempt_id=A1, entry_id="late")  # after its end: not its last event
    log.append("attempt_start", attempt_id=A2, pid=43)
    log.append("attempt_end", attempt_id=A2, status="failed: RuntimeError")
    one, two = attempts.attempts(attempts.read(log.path))
    assert (one.status, one.last_event, one.ended_at is not None) == (
        "killed", f"entry_start {ENTRY}", True,
    )  # fmt: skip
    assert (two.status, two.provenance_digest) == ("failed: RuntimeError", "")
    assert attempts.next_id(attempts.read(log.path), datetime(2026, 11, 17, 12, tzinfo=UTC)) == (
        "a003-20261117T120000Z"
    )


def test_started_entries_carry_their_trace_ids_and_whether_they_ended(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append("entry_start", attempt_id=A1, entry_id=ENTRY)
    log.append("entry_trace", attempt_id=A1, entry_id=ENTRY, trace_id=TRACE)
    log.append("entry_trace", attempt_id=A1, entry_id=ENTRY, trace_id=TRACE)
    log.append("entry_start", attempt_id=A1, entry_id="other")
    log.append("entry_end", attempt_id=A1, entry_id="other", status="no_trace")
    log.append("entry_start", attempt_id=A2, entry_id=ENTRY)
    log.append("entry_lost", attempt_id=A2, entry_id=ENTRY, reason="killed", trace_ids=[])
    started = attempts.started(attempts.read(log.path))
    assert [(s.attempt_id, s.entry_id, s.trace_ids, s.ended) for s in started] == [
        (A1, ENTRY, (TRACE,), False),
        (A1, "other", (), True),
        (A2, ENTRY, (), True),
    ]
    (lost,) = attempts.lost(attempts.read(log.path))
    assert (lost["attempt_id"], lost["reason"]) == (A2, "killed")


def test_abandoned_traces_are_validated_and_listed_once(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.abandoned(GONE)
    log.abandoned(attempts.Abandoned(**{**GONE.__dict__, "reason": "torn_tail"}))
    assert attempts.abandoned(attempts.read(log.path)) == [GONE]
    for change in (
        {"trace_id": "xyz"},
        {"eval_run_id": "a b"},
        {"entry_id": ""},
        {"attempt_id": "attempt one"},
        {"injection_id": "a b"},
        {"reason": "bored"},
        {"source": "a hunch"},
    ):
        with pytest.raises(attempts.AttemptsError):
            log.abandoned(attempts.Abandoned(**{**GONE.__dict__, **change}))
    assert len(attempts.read(log.path)) == 2
    clean = attempts.Abandoned(**{**GONE.__dict__, "planned_fault_kind": "", "injection_id": ""})
    assert attempts.check_abandoned(clean) is clean
