"""traceblame-faults record against a real ClickHouse (#14): the ledger into eval_traces and
injections through the contract statements, idempotently, and the join of an injection to the
span that carries its marker."""

from dataclasses import replace

import pytest

from traceblame_faults import ids, ledger, record
from traceblame_storage import contract
from traceblame_storage.migrations import packaged
from traceblame_storage.runner import migrate
from traceblame_storage.testing import ClickHouseServer

pytestmark = pytest.mark.integration

RUN = "faults-it-1"
QUERY = "Contract A__Governing Law"
SPAN_TRACE = "0af7651916cd43dd8448eb211c80319c"  # the trace of contract/spans_write.sql
CLEAN = ledger.Record(
    trace_id="1" * 32,
    eval_run_id=RUN,
    service="agentic-rag",
    source="cuad",
    query_ref=QUERY,
    stratum="answerable",
    golden_doc_ids=("cuad-0123456789ab-3", "cuad-0123456789ab-4"),
    golden_status="mapped",
    planned_fault_kind="",
    injection_id="",
    fault_kind="",
    target_component="",
    params={},
    seed=7,
    injected_at=None,
    bundle_digest="sha256:" + "d" * 64,
)
APPLIED = replace(
    CLEAN,
    trace_id=SPAN_TRACE,
    planned_fault_kind="malformed_output",
    injection_id=ids.injection_id(RUN, "malformed_output", QUERY),
    fault_kind="malformed_output",
    target_component="model",
    params={
        "scenario": "malformed_output-agentic-rag",
        "scenario_version": 1,
        "faults_version": "0.1.0",
        "mode": "fence",
        "cut_fraction": 0.5,
        "call_index": 1,
        "persist": "once",
    },
    injected_at="2026-11-06T10:00:00.123Z",
)
NOT_REACHED = replace(
    CLEAN,
    trace_id="2" * 32,
    planned_fault_kind="tool_failure",
    golden_doc_ids=(),
    golden_status="unmapped",
)


def _statement(name: str) -> str:
    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql


def test_record_is_idempotent_and_joins_the_marked_span(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        records = [CLEAN, APPLIED, NOT_REACHED]
        assert record.record(client, records) == {"eval_traces": 3, "injections": 1}
        assert record.record(client, records) == {"eval_traces": 3, "injections": 1}

        read = record.statement("eval_traces_read.sql")
        rows = client.query_rows(read, {"eval_run_id": RUN})
        assert [(bytes(r[0]).decode(), r[8], r[9]) for r in rows] == [
            (SPAN_TRACE, "malformed_output", APPLIED.injection_id),
            ("1" * 32, "", ""),
            ("2" * 32, "tool_failure", ""),
        ]
        assert rows[1][6] == ["cuad-0123456789ab-3", "cuad-0123456789ab-4"]
        assert rows[1][4] == QUERY
        count = client.query_rows("SELECT count() FROM injections")
        assert count == [(1,)], "re-recording adds no injection"
        (inj,) = client.query_rows(
            "SELECT fault_kind, target_component, params, seed, toString(injected_at)"
            " FROM injections"
        )
        assert inj[:2] == ("malformed_output", "model")
        assert inj[2].startswith('{"call_index":1,')
        assert inj[3:] == (7, "2026-11-06 10:00:00.123")

        span = _statement("spans_write.sql")
        last = "'server.port', '11434')"
        client.command(
            span.replace(
                last,
                f"'server.port', '11434', 'aegis.fault.injection.id', '{APPLIED.injection_id}')",
            )
        )
        join = _statement("injections_spans_read.sql")
        joined = client.query_rows(join, {"trace_id": SPAN_TRACE})
        assert [(r[0], r[1], bytes(r[2]).decode()) for r in joined] == [
            (APPLIED.injection_id, "malformed_output", "b7ad6b7169203331")
        ]
    finally:
        client.close()


def test_record_refuses_a_schema_without_eval_traces(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, [m for m in packaged() if m.name != "eval_traces"])
        with pytest.raises(record.RecordError, match="eval_traces"):
            record.record(client, [CLEAN])
        assert client.query_rows("SELECT count() FROM injections") == [(0,)]
    finally:
        client.close()
