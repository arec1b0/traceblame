"""Abandoned traces against a real ClickHouse (#70): `record` writes them to eval_exclusions
only; every reader of ground truth starts from eval_traces.trace_id, so an abandoned trace is in
none of them, even with its own spans, summary and an injections row under the same injection
id; the orphan sweep still sees it. N-1: without abandoned traces the code runs on the schema
before eval_exclusions; with them it refuses before writing.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from traceblame_detectors.export import export_snapshot
from traceblame_detectors.sources.snapshot import SPANS_FILE, SUMMARY_FILE
from traceblame_faults import attempts, dataset, ids, ledger, record
from traceblame_storage.migrations import packaged
from traceblame_storage.runner import migrate
from traceblame_storage.testing import ClickHouseServer

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
RUN = "faults-it-70"
QUERY = "Contract A__Governing Law"
DONE_TRACE = "a" * 32  # the trace that completed the faulted entry
GONE_TRACE = "b" * 32  # the trace in flight when the run was killed, same entry
CLEAN_TRACE = "c" * 32
ASSEMBLER = "1.0.0"
INJECTION = ids.injection_id(RUN, "malformed_output", QUERY)

CLEAN = ledger.Record(
    trace_id=CLEAN_TRACE,
    eval_run_id=RUN,
    service="agentic-rag",
    source="cuad",
    query_ref=QUERY,
    stratum="answerable",
    golden_doc_ids=("cuad-0123456789ab-3",),
    golden_status="mapped",
    planned_fault_kind="",
    injection_id="",
    fault_kind="",
    target_component="",
    params={},
    seed=19,
    entry_id=ids.entry_id("agentic-rag", "", QUERY),
    attempt_id="a002-20261117T110000Z",
)
APPLIED = replace(
    CLEAN,
    trace_id=DONE_TRACE,
    planned_fault_kind="malformed_output",
    injection_id=INJECTION,
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
    injected_at="2026-11-17T11:00:00.123Z",
    entry_id=ids.entry_id("agentic-rag", "malformed_output", QUERY),
)
GONE = attempts.Abandoned(
    trace_id=GONE_TRACE,
    eval_run_id=RUN,
    service="agentic-rag",
    entry_id=APPLIED.entry_id or "",
    attempt_id="a001-20261117T100000Z",
    planned_fault_kind="malformed_output",
    injection_id=INJECTION,
    reason="killed",
    source="attempt_log",
)

_SPAN = (
    "INSERT INTO spans (trace_id, span_id, parent_span_id, service, operation, span_kind,"
    " start_ts, duration_ms, status_code, tenant_id, attrs, profile_version, resource_attrs)"
    " VALUES ({trace_id:FixedString(32)}, {span_id:FixedString(16)}, '', 'agentic-rag', 'chat',"
    " 'llm', {start:DateTime64(6)}, 12.5, 'Ok', 'tenant-eval',"
    " if({marker:String} = '', map(), map('aegis.fault.injection.id', {marker:String})),"
    " '1.5.0', map('aegis.eval.run.id', {run:String}))"
)
_SUMMARY = (
    "INSERT INTO trace_summary (trace_id, assembler_version, revision, service, tenant_id,"
    " root_span_id, root_status_code, start_ts, duration_ms, span_count, error_span_count)"
    " VALUES ({trace_id:FixedString(32)}, {version:String}, now64(3), 'agentic-rag',"
    " 'tenant-eval', {span_id:FixedString(16)}, 'Ok', {start:DateTime64(6)}, 12.5, 1, 0)"
)


def _store_trace(client: object, trace_id: str, marker: str, start: str, run: str = RUN) -> None:
    """One stored span and the trace's summary, as the pipeline leaves them."""
    span_id = trace_id[:16]
    client.command(  # type: ignore[attr-defined]
        _SPAN,
        {
            "trace_id": trace_id,
            "span_id": span_id,
            "start": start,
            "marker": marker,
            "run": run,
        },
    )
    client.command(  # type: ignore[attr-defined]
        _SUMMARY,
        {"trace_id": trace_id, "version": ASSEMBLER, "span_id": span_id, "start": start},
    )


def _seed(client: object) -> None:
    """A killed and resumed run as it is stored: the completed traces recorded, the abandoned
    one in eval_exclusions, both faulted traces marked with the one injection id, and (the worst
    case, which record() itself never writes) an injections row for the abandoned trace too."""
    assert record.record(client, [CLEAN, APPLIED], [GONE]) == {  # type: ignore[arg-type]
        "eval_traces": 2,
        "injections": 1,
        "eval_exclusions": 1,
    }
    _store_trace(client, CLEAN_TRACE, "", "2026-11-17 10:59:00.000000")
    _store_trace(client, GONE_TRACE, INJECTION, "2026-11-17 10:00:00.000000")
    _store_trace(client, DONE_TRACE, INJECTION, "2026-11-17 11:00:00.000000")
    client.command(  # type: ignore[attr-defined]
        record.statement("injections_write.sql"),
        record.injection_params(replace(APPLIED, trace_id=GONE_TRACE)),
    )


def test_record_writes_abandoned_traces_to_eval_exclusions_only_and_idempotently(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        for _ in range(2):
            counts = record.record(client, [CLEAN, APPLIED], [GONE])
            assert counts == {"eval_traces": 2, "injections": 1, "eval_exclusions": 1}
        assert record.exclusions(client, RUN) == [GONE]
        assert record.exclusions(client, "another-run") == []
        labelled = client.query_rows(record.statement("eval_traces_read.sql"), {"eval_run_id": RUN})
        assert sorted(record.trace_id(r[0]) for r in labelled) == [DONE_TRACE, CLEAN_TRACE]
        on = client.query_rows("SELECT toString(trace_id) FROM injections")
        assert on == [(DONE_TRACE,)]
        with pytest.raises(record.RecordError, match="abandoned traces in the ledger"):
            record.record(client, [CLEAN, APPLIED], [replace(GONE, trace_id=DONE_TRACE)])
        with pytest.raises(record.RecordError, match="with an eval_traces row"):
            record.record_exclusions(client, [replace(GONE, trace_id=CLEAN_TRACE)])
        assert record.exclusions(client, RUN) == [GONE]
    finally:
        client.close()


def test_a_duplicated_injection_id_labels_exactly_one_trace(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        _seed(client)
        rows = dataset.injection_rows(client, [DONE_TRACE, CLEAN_TRACE], [INJECTION])
        assert sorted((t, i) for t, i, _ in rows) == [
            (DONE_TRACE, INJECTION),
            (GONE_TRACE, INJECTION),
        ], "the injection id has two injections rows"
        ds = dataset.read(client, RUN)
        labelled = [r for r in ds.rows if r.injection_id == INJECTION]
        assert [(r.trace_id, r.status, r.label) for r in labelled] == [
            (DONE_TRACE, "applied", "model")
        ]
        assert len(ds.rows) == 2
    finally:
        client.close()


def test_reader_the_d1_dataset_query_has_no_abandoned_trace(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        _seed(client)
        ds = dataset.read(client, RUN, splits={QUERY: "test"})
        assert ds.trace_ids == [DONE_TRACE, CLEAN_TRACE]
        assert GONE_TRACE not in {r["trace_id"] for r in dataset.labels(ds)}
        assert {r.split for r in ds.rows} == {"test"}
        counts = ds.counts()
        assert (counts["traces"], counts["excluded"]) == (2, 1)
        assert counts["excluded_by_reason"] == {"killed": 1}
        assert [a.trace_id for a in ds.excluded] == [GONE_TRACE]
    finally:
        client.close()


def test_reader_the_snapshot_export_has_no_abandoned_trace(
    clickhouse: ClickHouseServer, database: str, tmp_path: Path
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        _seed(client)
        ids_of_run = dataset.read(client, RUN).trace_ids
        manifest = export_snapshot(client, ids_of_run, ASSEMBLER, tmp_path / "d1", repo=ROOT)
        assert (manifest["trace_count"], manifest["span_count"]) == (2, 2)
        exported = {
            json.loads(line)["trace_id"]
            for name in (SPANS_FILE, SUMMARY_FILE)
            for line in (tmp_path / "d1" / name).read_text(encoding="utf-8").splitlines()
        }
        assert exported == {DONE_TRACE, CLEAN_TRACE}
        # The abandoned trace is stored whole and would export: only the id list keeps it out.
        alone = export_snapshot(client, [GONE_TRACE], ASSEMBLER, tmp_path / "gone", repo=ROOT)
        assert (alone["trace_count"], alone["span_count"]) == (1, 1)
    finally:
        client.close()


def test_the_orphan_sweep_reads_every_stored_trace_of_the_run_with_its_markers(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        _seed(client)
        _store_trace(client, "d" * 32, "", "2026-11-17 10:30:00.000000", run="another-run")
        _store_trace(client, "e" * 32, "", "2026-11-01 10:30:00.000000")  # before the run began
        assert dataset.stored_traces(client, RUN, "runbook-agent", "2026-11-17 00:00:00") == []
        stored = dataset.stored_traces(client, RUN, "agentic-rag", "2026-11-17 00:00:00")
        assert [(t, sorted(m), n) for t, _, m, n in stored] == [
            (DONE_TRACE, [INJECTION], 1),
            (GONE_TRACE, [INJECTION], 1),
            (CLEAN_TRACE, [], 1),
        ]
        first = {t: start for t, start, _, _ in stored}
        assert first[DONE_TRACE] - first[GONE_TRACE] == 3600.0
        labelled = {r.trace_id for r in dataset.read(client, RUN).rows}
        excluded = {a.trace_id for a in record.exclusions(client, RUN)}
        assert {t for t, *_ in stored} - labelled - excluded == set()
        assert {t for t, *_ in stored} - labelled == {GONE_TRACE}
    finally:
        client.close()


def test_n_minus_1_without_abandoned_traces_the_code_runs_before_eval_exclusions(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, [m for m in packaged() if m.name != "eval_exclusions"])
        assert record.record(client, [CLEAN, APPLIED]) == {
            "eval_traces": 2,
            "injections": 1,
            "eval_exclusions": 0,
        }
        assert record.exclusions(client, RUN) == []
        ds = dataset.read(client, RUN)
        assert (ds.trace_ids, ds.excluded) == ([DONE_TRACE, CLEAN_TRACE], ())
    finally:
        client.close()


def test_n_minus_1_with_abandoned_traces_record_refuses_before_writing_anything(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, [m for m in packaged() if m.name != "eval_exclusions"])
        with pytest.raises(record.RecordError, match=r"eval_exclusions.*migrate"):
            record.record(client, [CLEAN, APPLIED], [GONE])
        assert client.query_rows("SELECT count() FROM eval_traces") == [(0,)]
        assert client.query_rows("SELECT count() FROM injections") == [(0,)]
    finally:
        client.close()


def test_the_new_statements_run_with_their_sample_parameters(
    clickhouse: ClickHouseServer, database: str
) -> None:
    from traceblame_storage import contract

    names = [
        "eval_exclusions_write.sql",
        "eval_exclusions_read.sql",
        "eval_dataset_read.sql",
        "eval_run_traces_read.sql",
        "eval_injections_by_id_read.sql",
    ]
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        for name in names:
            sql = (ROOT / "tests" / "integration" / name).read_text(encoding="utf-8")
            if name.endswith("_write.sql"):
                client.command(sql, contract.parameters(sql))
            else:
                client.query_rows(sql, parameters=contract.parameters(sql))
        assert len(names) == 5
    finally:
        client.close()
