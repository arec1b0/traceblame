"""record(): the ledger into injections and eval_traces through the storage contract
statements, idempotently. The statements themselves run against ClickHouse in
tests/integration/test_faults_record_live.py."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from traceblame_faults import ids, ledger, record

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
DIGEST = "sha256:" + "c" * 64
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
    trace_id="5bf92f3577b34da6a3ce929d0e0e4736",
    planned_fault_kind="tool_failure",
    injection_id=ids.injection_id(
        "faults-20261106-s7-abc1234", "tool_failure", "Contract A__Governing Law"
    ),
    fault_kind="tool_failure",
    target_component="tool",
    params={
        "scenario": "tool_failure-agentic-rag",
        "scenario_version": 1,
        "faults_version": "0.1.0",
        "mode": "backend_error",
        "tool": "retriever",
        "call_index": 1,
    },
    injected_at="2026-11-06T10:00:00.123Z",
)


class FakeClient:
    """Records statements; answers the table probe (a test double)."""

    def __init__(self, tables: set[str]) -> None:
        self.tables = tables
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object:
        self.commands.append((sql, dict(parameters or {})))
        return None

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        return [(t,) for t in sorted(self.tables)]


def test_every_trace_goes_to_eval_traces_and_applied_ones_to_injections() -> None:
    client = FakeClient({"injections", "eval_traces"})
    counts = record.record(client, [CLEAN, FAULTED])
    assert counts == {"eval_traces": 2, "injections": 1}
    inserts = [(s.split("(")[0].split()[-1], p) for s, p in client.commands]
    assert [t for t, _ in inserts] == ["eval_traces", "eval_traces", "injections"]
    _, inj = inserts[2]
    assert inj["trace_id"] == FAULTED.trace_id
    assert inj["injection_id"] == FAULTED.injection_id
    assert inj["target_component"] == "tool"
    assert inj["params"] == (
        '{"call_index":1,"faults_version":"0.1.0","mode":"backend_error",'
        '"scenario":"tool_failure-agentic-rag","scenario_version":1,"tool":"retriever"}'
    )
    assert inj["injected_at"] == "2026-11-06 10:00:00.123"
    assert inserts[0][1]["golden_doc_ids"] == ["cuad-0123456789ab-3"]
    assert inserts[1][1]["injection_id"] == FAULTED.injection_id


def test_the_statements_are_the_contract_ones() -> None:
    client = FakeClient({"injections", "eval_traces"})
    record.record(client, [FAULTED])
    assert client.commands[-1][0] == record.statement("injections_write.sql")
    assert client.commands[0][0] == record.statement("eval_traces_write.sql")


def test_a_schema_without_eval_traces_is_refused_before_any_write() -> None:
    client = FakeClient({"injections"})
    with pytest.raises(record.RecordError, match="eval_traces"):
        record.record(client, [CLEAN])
    assert client.commands == []


def test_invalid_records_are_refused_before_any_write() -> None:
    client = FakeClient({"injections", "eval_traces"})
    with pytest.raises(ledger.LedgerError):
        record.record(client, [CLEAN, replace(FAULTED, params={"text": "x"})])
    assert client.commands == []
