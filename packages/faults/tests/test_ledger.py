"""The ledger the injector writes in-process: one record per eval trace, content-free."""

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
