"""The ledger the injector writes in-process: one record per eval trace, content-free."""

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from traceblame_faults import ids, ledger

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
DIGEST = "sha256:" + "b" * 64

CLEAN = ledger.Record(
    trace_id=TRACE,
    eval_run_id="faults-20261106-s7-abc1234",
    service="agentic-rag",
    source="cuad",
    query_ref="Contract A__Governing Law",
    stratum="answerable",
    golden_doc_ids=("cuad-0123456789ab-3",),
    golden_status="mapped",
    planned_fault_kind="",
    injection_id="",
    fault_kind="",
    target_component="",
    params={},
    seed=7,
    injected_at=None,
    bundle_digest=DIGEST,
)
FAULTED = replace(
    CLEAN,
    planned_fault_kind="malformed_output",
    injection_id=ids.injection_id(
        "faults-20261106-s7-abc1234", "malformed_output", "Contract A__Governing Law"
    ),
    fault_kind="malformed_output",
    target_component="model",
    params={
        "scenario": "malformed_output-agentic-rag",
        "scenario_version": 1,
        "faults_version": "0.1.0",
        "mode": "truncate",
        "cut_fraction": 0.5,
        "call_index": 1,
        "persist": "once",
        "baseline_digest": DIGEST,
    },
    injected_at="2026-11-06T10:00:00.123Z",
)
NOT_REACHED = replace(CLEAN, planned_fault_kind="tool_failure")


@pytest.mark.parametrize("rec", [CLEAN, FAULTED, NOT_REACHED])
def test_valid_records_pass_and_round_trip(rec: ledger.Record, tmp_path: Path) -> None:
    ledger.validate(rec)
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, rec)
    ledger.append(path, rec)
    assert ledger.read(path) == [rec, rec]


def test_status() -> None:
    assert ledger.status(CLEAN) == "clean"
    assert ledger.status(FAULTED) == "applied"
    assert ledger.status(NOT_REACHED) == "not_reached"


@pytest.mark.parametrize(
    ("rec", "match"),
    [
        (replace(CLEAN, trace_id="xyz"), "trace_id"),
        (replace(CLEAN, eval_run_id="a b"), "eval_run_id"),
        (replace(CLEAN, golden_status="maybe"), "golden_status"),
        (replace(CLEAN, golden_status="none"), "golden_doc_ids"),
        (replace(CLEAN, golden_doc_ids=("the term is five years",)), "golden_doc_ids"),
        (replace(CLEAN, query_ref="line\nbreak"), "query_ref"),
        (replace(CLEAN, params={"mode": "truncate"}), "params"),
        (replace(CLEAN, bundle_digest="You are a helpful assistant"), "bundle_digest"),
        (replace(FAULTED, fault_kind="tool_failure"), "fault_kind"),
        (replace(FAULTED, target_component="tool"), "target_component"),
        (replace(FAULTED, injected_at=None), "injected_at"),
        (replace(FAULTED, params={**FAULTED.params, "note": "free text"}), "note"),
        (replace(FAULTED, injection_id="other:malformed_output:0123456789ab"), "injection_id"),
        (replace(NOT_REACHED, fault_kind="tool_failure"), "fault_kind"),
        (replace(CLEAN, host_load1=-0.5), "host_load1"),
        (replace(CLEAN, host_load1=float("nan")), "host_load1"),
        (replace(CLEAN, host_load1=float("inf")), "host_load1"),
    ],
)
def test_invalid_records_are_refused(rec: ledger.Record, match: str) -> None:
    with pytest.raises(ledger.LedgerError, match=match):
        ledger.validate(rec)


def test_read_validates_every_line(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(ledger.dumps(CLEAN) + "\n" + '{"trace_id": "x"}\n', encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="line 2"):
        ledger.read(path)


def test_host_load1_rides_along_and_old_lines_load_without_it(tmp_path: Path) -> None:
    rec = replace(FAULTED, host_load1=12.34)
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, rec)
    old = json.loads(ledger.dumps(CLEAN))
    del old["host_load1"]  # a line written before the field existed (the #14 validation run)
    path.write_text(path.read_text(encoding="utf-8") + json.dumps(old) + "\n", encoding="utf-8")
    assert ledger.read(path) == [rec, CLEAN]
    assert CLEAN.host_load1 is None


def test_host_load1_samples_the_one_minute_load_average(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getloadavg", lambda: (7.256, 5.0, 4.0))
    assert ledger.host_load1() == 7.26


def test_host_load1_is_none_where_the_platform_has_no_load_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> tuple[float, float, float]:
        raise OSError("no load average")

    monkeypatch.setattr(os, "getloadavg", unavailable)
    assert ledger.host_load1() is None


# Resume (#70): durable appends, one record per plan entry, torn tails ------------------------

ENTRY = ids.entry_id("agentic-rag", "", CLEAN.query_ref)
WITH_ENTRY = replace(CLEAN, entry_id=ENTRY, attempt_id="a001-20261117T100000Z")
OTHER_TRACE = "0af7651916cd43dd8448eb211c80319c"


def test_a_record_names_its_plan_entry_and_attempt_and_old_lines_load_without_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, WITH_ENTRY)
    old = json.loads(ledger.dumps(FAULTED))
    del old["entry_id"], old["attempt_id"]  # a #14 ledger line
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(old) + "\n")
    assert ledger.read(path) == [WITH_ENTRY, FAULTED]
    assert (FAULTED.entry_id, FAULTED.attempt_id) == (None, None)


@pytest.mark.parametrize(
    ("rec", "match"),
    [
        (replace(CLEAN, entry_id="agentic-rag:clean:0123456789ab"), "entry_id"),
        (replace(FAULTED, entry_id=ENTRY), "entry_id"),
        (replace(WITH_ENTRY, attempt_id="attempt one"), "attempt_id"),
    ],
)
def test_an_entry_id_must_derive_from_the_record(rec: ledger.Record, match: str) -> None:
    with pytest.raises(ledger.LedgerError, match=match):
        ledger.validate(rec)


def test_append_is_one_write_on_o_append_followed_by_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, object]] = []
    real_open, real_write, real_fsync = os.open, os.write, os.fsync

    def spy_open(path: object, flags: int, *args: object) -> int:
        calls.append(("open", flags))
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    def spy_write(fd: int, data: bytes) -> int:
        calls.append(("write", bytes(data)))
        return real_write(fd, data)

    def spy_fsync(fd: int) -> None:
        calls.append(("fsync", None))
        real_fsync(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "write", spy_write)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b"")  # exists: no directory fsync to tell apart
    ledger.append(path, WITH_ENTRY)
    names = [name for name, _ in calls]
    assert names == ["open", "write", "fsync"]
    assert int(calls[0][1]) & os.O_APPEND  # type: ignore[call-overload]
    assert calls[1][1] == (ledger.dumps(WITH_ENTRY) + "\n").encode()


def test_the_ledger_is_never_truncated_by_an_append(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, WITH_ENTRY)
    before = path.read_bytes()
    ledger.append(path, replace(FAULTED, trace_id=OTHER_TRACE))
    assert path.read_bytes().startswith(before)


def test_a_writer_refuses_a_second_record_for_an_entry(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    writer = ledger.Writer(path)
    writer.append(WITH_ENTRY)
    with pytest.raises(ledger.LedgerError, match="already has a record"):
        writer.append(replace(WITH_ENTRY, trace_id=OTHER_TRACE))
    # A new writer on the same file (the next attempt) knows the entry too.
    with pytest.raises(ledger.LedgerError, match="already has a record"):
        ledger.Writer(path).append(replace(WITH_ENTRY, trace_id=OTHER_TRACE))
    with pytest.raises(ledger.LedgerError, match="entry_id"):
        ledger.Writer(path).append(CLEAN)
    assert ledger.read(path) == [WITH_ENTRY]


def test_two_records_for_one_entry_in_a_file_are_refused_on_read(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, WITH_ENTRY)
    ledger.append(path, replace(WITH_ENTRY, trace_id=OTHER_TRACE))  # not through a Writer
    with pytest.raises(ledger.LedgerError, match="two records"):
        ledger.read(path)


@pytest.mark.parametrize(
    "tail",
    [
        b'{"trace_id": "4bf9',  # half a record
        ledger.dumps(replace(FAULTED, trace_id=OTHER_TRACE)).encode(),  # whole JSON, no newline
    ],
)
def test_a_torn_tail_is_refused_by_every_reader_and_writer(tmp_path: Path, tail: bytes) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, WITH_ENTRY)
    with path.open("ab") as f:
        f.write(tail)
    assert ledger.torn(path) is not None
    with pytest.raises(ledger.LedgerError, match="torn tail"):
        ledger.read(path)
    with pytest.raises(ledger.LedgerError, match="torn tail"):
        ledger.append(path, FAULTED)
    with pytest.raises(ledger.LedgerError, match="torn tail"):
        ledger.Writer(path)


def test_repair_copies_the_torn_tail_aside_and_cuts_it_off(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger.append(path, WITH_ENTRY)
    whole = path.read_bytes()
    tail = b'{"trace_id": "4bf9'
    path.write_bytes(whole + tail)
    copy = tmp_path / "ledger.torn.a002"
    found = ledger.repair(path, copy)
    assert found is not None
    assert (found.offset, found.length) == (len(whole), len(tail))
    assert copy.read_bytes() == tail
    assert path.read_bytes() == whole
    assert ledger.read(path) == [WITH_ENTRY]
    assert ledger.repair(path, tmp_path / "ledger.torn.a003") is None
    assert not (tmp_path / "ledger.torn.a003").exists()


def test_a_missing_ledger_reads_as_no_records(tmp_path: Path) -> None:
    assert ledger.read(tmp_path / "ledger.jsonl") == []
    assert ledger.torn(tmp_path / "ledger.jsonl") is None
