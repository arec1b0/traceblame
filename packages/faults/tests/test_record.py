"""record(): the ledger into injections and eval_traces through the storage contract
statements, idempotently. The statements themselves run against ClickHouse in
tests/integration/test_faults_record_live.py."""

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from traceblame_faults import attempts, dataset, ids, ledger, record

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
    """Records statements; answers the table probe and the reads it was given (a test double)."""

    def __init__(self, tables: set[str], reads: dict[str, list[tuple[Any, ...]]] | None = None):
        self.tables = tables
        self.reads = reads or {}
        self.commands: list[tuple[str, dict[str, Any]]] = []

    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object:
        self.commands.append((sql, dict(parameters or {})))
        return None

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if "system.tables" in sql:
            return [(t,) for t in sorted(self.tables)]
        for name, rows in self.reads.items():
            if sql == record.statement(name):
                return rows
        return []


def test_every_trace_goes_to_eval_traces_and_applied_ones_to_injections() -> None:
    client = FakeClient({"injections", "eval_traces"})
    counts = record.record(client, [CLEAN, FAULTED])
    assert counts == {"eval_traces": 2, "injections": 1, "eval_exclusions": 0}
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


def test_a_not_reached_retrieval_fault_gets_an_eval_trace_and_no_injection() -> None:
    # golden_context_removed in mode "remove" whose run never had a golden chunk to remove.
    not_reached = replace(
        CLEAN,
        trace_id="6bf92f3577b34da6a3ce929d0e0e4736",
        planned_fault_kind="golden_context_removed",
    )
    assert ledger.status(not_reached) == "not_reached"
    client = FakeClient({"injections", "eval_traces"})
    assert record.record(client, [not_reached]) == {
        "eval_traces": 1,
        "injections": 0,
        "eval_exclusions": 0,
    }
    (sql, params), *rest = client.commands
    assert sql.split("(")[0].split()[-1] == "eval_traces"
    assert (params["planned_fault_kind"], params["injection_id"]) == ("golden_context_removed", "")
    assert rest == []


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


# Abandoned traces (#70) -------------------------------------------------------------------

SCHEMA = {"injections", "eval_traces", "eval_exclusions"}
GONE = attempts.Abandoned(
    trace_id="7bf92f3577b34da6a3ce929d0e0e4736",
    eval_run_id=CLEAN.eval_run_id,
    service="agentic-rag",
    entry_id=ids.entry_id("agentic-rag", "tool_failure", CLEAN.query_ref),
    attempt_id="a001-20261117T100000Z",
    planned_fault_kind="tool_failure",
    injection_id=FAULTED.injection_id,
    reason="killed",
    source="attempt_log",
)


def test_an_abandoned_trace_goes_to_eval_exclusions_and_nowhere_else() -> None:
    client = FakeClient(SCHEMA)
    counts = record.record(client, [CLEAN, FAULTED], [GONE])
    assert counts == {"eval_traces": 2, "injections": 1, "eval_exclusions": 1}
    by_table: dict[str, list[dict[str, Any]]] = {}
    for sql, params in client.commands:
        by_table.setdefault(sql.split("(")[0].split()[-1], []).append(params)
    assert [p["trace_id"] for p in by_table["eval_exclusions"]] == [GONE.trace_id]
    assert GONE.trace_id not in {p["trace_id"] for p in by_table["eval_traces"]}
    assert GONE.trace_id not in {p["trace_id"] for p in by_table["injections"]}
    assert by_table["eval_exclusions"][0] == {
        "trace_id": GONE.trace_id,
        "eval_run_id": GONE.eval_run_id,
        "service": "agentic-rag",
        "entry_id": GONE.entry_id,
        "attempt_id": GONE.attempt_id,
        "planned_fault_kind": "tool_failure",
        "injection_id": FAULTED.injection_id,
        "reason": "killed",
        "source": "attempt_log",
    }
    assert client.commands[-1][0] == record.statement("eval_exclusions_write.sql")


def test_a_ledger_that_names_an_abandoned_trace_is_refused_before_any_write() -> None:
    client = FakeClient(SCHEMA)
    with pytest.raises(record.RecordError, match="abandoned traces in the ledger"):
        record.record(client, [CLEAN, FAULTED], [replace(GONE, trace_id=FAULTED.trace_id)])
    assert client.commands == []


def test_without_abandoned_traces_record_runs_on_a_schema_without_eval_exclusions() -> None:
    client = FakeClient({"injections", "eval_traces"})  # schema N-1
    assert record.record(client, [CLEAN])["eval_exclusions"] == 0
    assert record.exclusions(client, CLEAN.eval_run_id) == []


def test_with_abandoned_traces_a_schema_without_eval_exclusions_is_refused_before_any_write() -> (
    None
):
    client = FakeClient({"injections", "eval_traces"})
    with pytest.raises(record.RecordError, match="eval_exclusions"):
        record.record(client, [CLEAN, FAULTED], [GONE])
    assert client.commands == []
    with pytest.raises(record.RecordError, match="eval_exclusions"):
        record.record_exclusions(client, [GONE])


def test_an_invalid_abandoned_trace_is_refused_before_any_write() -> None:
    client = FakeClient(SCHEMA)
    for bad in (replace(GONE, trace_id="xyz"), replace(GONE, reason="bored"),
                replace(GONE, entry_id="a b"), replace(GONE, injection_id="a b")):  # fmt: skip
        with pytest.raises(attempts.AttemptsError):
            record.record(client, [CLEAN], [bad])
    assert client.commands == []


def test_an_exclusion_for_a_trace_with_an_eval_traces_row_is_refused() -> None:
    row: tuple[Any, ...] = (GONE.trace_id.encode(), GONE.eval_run_id, "agentic-rag", "cuad",
                            "q", "s", [], "none", "", "", 7)  # fmt: skip
    client = FakeClient(SCHEMA, {"eval_traces_read.sql": [row]})
    with pytest.raises(record.RecordError, match="with an eval_traces row"):
        record.record_exclusions(client, [GONE])
    assert client.commands == []
    assert record.record_exclusions(client, []) == 0


def test_exclusions_read_back_as_abandoned_traces() -> None:
    row = (GONE.trace_id.encode(), *list(GONE.__dict__.values())[1:])
    client = FakeClient(SCHEMA, {"eval_exclusions_read.sql": [row]})
    assert record.exclusions(client, GONE.eval_run_id) == [GONE]


# The dataset read (#70): from eval_traces.trace_id, never by injection id alone -----------


def _row(rec: ledger.Record, *, rows: int | None = None, kind: str | None = None,
         variants: int = 1) -> tuple[Any, ...]:  # fmt: skip
    applied = ledger.status(rec) == "applied"
    n = rows if rows is not None else int(applied)
    return (
        rec.trace_id.encode(), rec.service, rec.source, rec.query_ref, rec.stratum,
        rec.planned_fault_kind, rec.injection_id,
        (kind if kind is not None else rec.fault_kind) if n else "",
        rec.target_component if n else "", n, variants if n else 0,
    )  # fmt: skip


NOT_REACHED = replace(
    CLEAN, trace_id="6bf92f3577b34da6a3ce929d0e0e4736", planned_fault_kind="golden_context_removed"
)


def _dataset(rows: list[tuple[Any, ...]], excluded: list[attempts.Abandoned] | None = None,
             **kwargs: Any) -> dataset.Dataset:  # fmt: skip
    reads: dict[str, list[tuple[Any, ...]]] = {"eval_dataset_read.sql": rows}
    if excluded:
        reads["eval_exclusions_read.sql"] = [
            (a.trace_id.encode(), *list(a.__dict__.values())[1:]) for a in excluded
        ]
    return dataset.read(FakeClient(SCHEMA, reads), CLEAN.eval_run_id, **kwargs)


def test_the_dataset_labels_clean_applied_and_not_reached_traces() -> None:
    ds = _dataset([_row(CLEAN), _row(FAULTED), _row(NOT_REACHED)], [GONE])
    assert [(r.trace_id, r.status, r.label) for r in ds.rows] == [
        (CLEAN.trace_id, "clean", "clean"),
        (FAULTED.trace_id, "applied", "tool"),
        (NOT_REACHED.trace_id, "not_reached", None),
    ]
    assert ds.trace_ids == sorted([CLEAN.trace_id, FAULTED.trace_id, NOT_REACHED.trace_id])
    assert GONE.trace_id not in ds.trace_ids
    assert ds.counts() == {
        "traces": 3,
        "by_status": {"clean": 1, "applied": 1, "not_reached": 1},
        "by_label": {"clean": 1, "tool": 1, "not_reached": 1},
        "by_split": {"none": 3},
        "excluded": 1,
        "excluded_by_reason": {"killed": 1},
        "excluded_by_kind": {"tool_failure": 1},
    }
    assert dataset.labels(ds)[1] == {
        "trace_id": FAULTED.trace_id,
        "service": "agentic-rag",
        "query_ref": FAULTED.query_ref,
        "planned_fault_kind": "tool_failure",
        "status": "applied",
        "label": "tool",
        "split": "",
    }


def test_the_dataset_carries_each_querys_split_and_refuses_a_query_without_one() -> None:
    ds = _dataset([_row(CLEAN), _row(FAULTED)], splits={CLEAN.query_ref: "test"})
    assert {r.split for r in ds.rows} == {"test"}
    with pytest.raises(dataset.DatasetError, match="no split side"):
        _dataset([_row(CLEAN)], splits={"Another__Query": "calib"})


@pytest.mark.parametrize(
    ("rows", "excluded", "match"),
    [
        ([], [], "nothing read"),
        ([_row(CLEAN), _row(CLEAN)], [], "two eval_traces rows"),
        ([_row(FAULTED)], [replace(GONE, trace_id=FAULTED.trace_id)], "in eval_exclusions"),
        ([_row(FAULTED), _row(replace(FAULTED, trace_id=GONE.trace_id))], [], "labels two traces"),
        ([_row(FAULTED, rows=0)], [], "no injections row on this trace"),
        ([_row(FAULTED, variants=2)], [], "do not agree"),
        ([_row(FAULTED, kind="malformed_output")], [], "do not agree"),
        ([_row(replace(CLEAN, injection_id="x"))], [], "clean trace with an injection id"),
    ],
)
def test_the_dataset_read_fails_rather_than_return_a_wrong_label(
    rows: list[tuple[Any, ...]], excluded: list[attempts.Abandoned], match: str
) -> None:
    with pytest.raises(dataset.DatasetError, match=match):
        _dataset(rows, excluded)


def test_repeated_rows_of_one_injection_on_its_own_trace_still_label_it_once() -> None:
    ds = _dataset([_row(FAULTED, rows=2)])
    assert [(r.status, r.label) for r in ds.rows] == [("applied", "tool")]


def test_the_stored_traces_and_injection_reads_decode_their_rows() -> None:
    from datetime import UTC, datetime

    start = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
    reads: dict[str, list[tuple[Any, ...]]] = {
        "eval_run_traces_read.sql": [
            (CLEAN.trace_id.encode(), start.replace(tzinfo=None), [], 3),
            (FAULTED.trace_id.encode(), "2026-11-17 10:00:01.500000", [FAULTED.injection_id], 5),
        ],
        "eval_injections_by_id_read.sql": [(FAULTED.trace_id, FAULTED.injection_id, "{}")],
    }
    client = FakeClient(SCHEMA, reads)
    assert dataset.stored_traces(
        client, CLEAN.eval_run_id, "agentic-rag", "2026-11-17 00:00:00"
    ) == [
        (CLEAN.trace_id, start.timestamp(), frozenset(), 3),
        (FAULTED.trace_id, start.timestamp() + 1.5, frozenset({FAULTED.injection_id}), 5),
    ]
    assert dataset.injection_rows(client, [FAULTED.trace_id], [FAULTED.injection_id]) == [
        (FAULTED.trace_id, FAULTED.injection_id, "{}")
    ]
