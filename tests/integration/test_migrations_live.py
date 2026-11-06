"""The migrations against a real ClickHouse: tables, idempotency, checksums, N-1."""

import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from traceblame_storage import contract
from traceblame_storage.migrations import Migration, at_revision, packaged
from traceblame_storage.runner import ChecksumMismatchError, DropGuardError, migrate
from traceblame_storage.testing import ClickHouseServer

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration

# Far beyond any arrival: the upper bound of a scan that wants everything after w0.
FAR_MS = 4_102_444_800_000


def _recent_ms() -> int:
    """An hour ago: trace_arrivals expires a row 2 days after its arrival day, and ClickHouse
    drops expired rows at insert, so test rows must carry recent stamps."""
    return int(datetime.now(UTC).timestamp() * 1000) - 3_600_000


# The fault injector's write before injections.injection_id (0013): main's contract statement
# until #14 moved the injector's write, with the column, into contract/. Code N-1 on schema N-1.
INJECTION_WRITE_BEFORE_0013 = (
    "INSERT INTO injections (trace_id, fault_kind, target_component, params, seed, injected_at)"
    " VALUES ('0af7651916cd43dd8448eb211c80319c', 'context_truncation', 'orchestration', '{}', 7,"
    " now64(3))"
)
INJECTION_ID_MIGRATION = "injections_injection_id"

ENGINES = {
    "spans": "MergeTree",
    "otel_traces_ingest": "Null",
    "spans_from_otel": "MaterializedView",
    "trace_arrivals": "MergeTree",
    "trace_arrivals_from_otel": "MaterializedView",
    "trace_summary": "ReplacingMergeTree",
    "findings": "ReplacingMergeTree",
    "blame": "ReplacingMergeTree",
    "injections": "MergeTree",
    "eval_traces": "ReplacingMergeTree",
    "detector_runs": "ReplacingMergeTree",
    "detector_params": "ReplacingMergeTree",
    "schema_migrations": "MergeTree",
}


def test_migrate_creates_every_table_once(clickhouse: ClickHouseServer, database: str) -> None:
    client = clickhouse.client(database)
    try:
        assert [m.version for m in migrate(client, packaged())] == [m.version for m in packaged()]
        assert migrate(client, packaged()) == [], "a second run applies nothing"
        rows = client.query_rows(
            f"SELECT name, engine FROM system.tables WHERE database = '{database}'"  # noqa: S608
        )
        assert dict(rows) == ENGINES
    finally:
        client.close()


def test_a_changed_applied_migration_is_refused(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        edited = [replace(packaged()[0], sql=packaged()[0].sql + "\n-- edited")]
        with pytest.raises(ChecksumMismatchError):
            migrate(client, edited)
    finally:
        client.close()


def test_the_contract_statements_run_on_the_schema(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        results: dict[str, list[tuple[Any, ...]]] = {}
        for name, sql in contract.packaged():
            if name.endswith("_write.sql"):
                client.command(sql, contract.parameters(sql))
            else:
                results[name] = client.query_rows(sql, parameters=contract.parameters(sql))
        (span,) = results["spans_read.sql"]
        cols = [
            "trace_id",
            "span_id",
            "parent_span_id",
            "service",
            "operation",
            "span_kind",
            "start_ts",
            "duration_ms",
            "status_code",
            "provider",
            "model",
            "finish_reasons",
            "input_tokens",
            "output_tokens",
            "tool_name",
            "args_hash",
            "retrieval_scores",
            "retrieved_doc_ids",
            "bundle_digest",
            "tenant_id",
            "session_id",
            "content_ref",
            "attrs",
            "profile_version",
            "decision_id",
            "error_type",
            "http_status_code",
            "source_sdk",
            "normalizer_version",
            "resource_attrs",
        ]
        row = dict(zip(cols, span, strict=True))
        assert row["span_kind"] == "llm"
        assert row["operation"] == "chat"
        assert row["model"] == "qwen2.5:7b"
        assert row["finish_reasons"] == ["stop"]
        assert (row["input_tokens"], row["output_tokens"]) == (38, 2)
        assert (row["error_type"], row["http_status_code"]) == ("BadRequestError", 400)
        assert row["tenant_id"] == "contract"
        assert row["duration_ms"] == 1.0
        assert row["attrs"] == {"server.port": "11434"}, "only attributes without a column"
        assert row["resource_attrs"] == {"service.version": "1.0.0"}
        assert len(results["findings_read.sql"]) == 1
        assert len(results["blame_read.sql"]) == 1
        assert len(results["injections_read.sql"]) == 1
        assert results["injections_spans_read.sql"] == [], "no span carries the marker here"
    finally:
        client.close()


def _statement(name: str) -> str:
    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql


def _index_section(plan: str, index: str) -> str:
    """The lines of one index (Min-Max, Partition, PrimaryKey) in EXPLAIN indexes = 1."""
    assert index in plan, plan
    return plan.split(index, 1)[1].split("Granules", 1)[0]


def _scan_params(w0: int, w1: int = FAR_MS, count: int = 1, partition: int = 0) -> dict[str, int]:
    return {"w0": w0, "w1": w1, "partition_count": count, "partition": partition}


def _scan(
    client: Any, w0: int, w1: int = FAR_MS, count: int = 1, partition: int = 0
) -> list[tuple[Any, ...]]:
    """The watermark scan (contract/trace_arrivals_scan.sql) of one window of one partition."""
    return client.query_rows(
        _statement("trace_arrivals_scan.sql"), parameters=_scan_params(w0, w1, count, partition)
    )


def _arrive(client: Any, trace: str, at_ms: int, *, root: bool) -> None:
    """A trace_arrivals row with an explicit arrival time (the view stamps the insert's own)."""
    at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=at_ms)
    client.insert(
        "trace_arrivals",
        [[trace, at, root, "svc", "contract"]],
        ["trace_id", "ingested_at", "is_root", "service", "tenant_id"],
    )


def _late_child_write() -> str:
    """spans_write.sql as a child of its own span, exported in a later insert."""
    sql = _statement("spans_write.sql")
    root = "'b7ad6b7169203331', ''"
    assert sql.count(root) == 1
    return sql.replace(root, "'c0ffee00c0ffee00', 'b7ad6b7169203331'")


def test_the_watermark_scan_finds_root_and_late_arrivals_by_primary_key(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """ADR-0003: one scan over (w0, w1]; a root arrival is assembled, a later non-root arrival
    re-assembles; the scan prunes on ingested_at, never a full scan."""
    trace = "0af7651916cd43dd8448eb211c80319c"
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        client.command(_statement("spans_write.sql"))
        (first,) = _scan(client, 0)
        assert first[:2] == (trace, True), "root: assemble"
        watermark = first[2]

        client.command(_late_child_write())
        (late,) = _scan(client, watermark)
        assert late[:2] == (trace, False), (
            "late non-root arrival of an assembled trace: re-assemble"
        )
        assert late[2] > watermark, "a later revision"
        (whole,) = _scan(client, 0)
        assert whole[:2] == (trace, True)
        assert whole[2] == late[2], "the revision is the newest arrival"

        plan = "\n".join(
            str(r[0])
            for r in client.query_rows(
                f"EXPLAIN indexes = 1 {_statement('trace_arrivals_scan.sql')}",
                parameters=_scan_params(watermark),
            )
        )
        minmax = _index_section(plan, "Min-Max")
        assert "ingested_at" in minmax, plan
        assert "Parts: 1/2" in minmax, "the root's part is before the watermark: pruned"
        primary = _index_section(plan, "PrimaryKey")
        assert "ingested_at" in primary, plan
        assert "Condition: true" not in primary, plan
    finally:
        client.close()


def test_consecutive_windows_see_an_arrival_stamped_at_the_edge_once(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """(w0, w1] then (w1, w2]: an arrival stamped exactly at w1 is in the first window only."""
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        t = _recent_ms()
        _arrive(client, "a" * 32, t + 1_000, root=True)
        _arrive(client, "b" * 32, t + 2_000, root=True)  # exactly w1
        _arrive(client, "c" * 32, t + 2_001, root=True)
        first = _scan(client, t, t + 2_000)
        second = _scan(client, t + 2_000, t + 3_000)
        assert [r[0] for r in first] == ["a" * 32, "b" * 32]
        assert [r[0] for r in second] == ["c" * 32]
        assert first[1][2] == t + 2_000
    finally:
        client.close()


def test_the_partitions_of_a_count_split_the_window_without_overlap(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """cityHash64(trace_id) % N: every trace is in exactly one of the N partition scans."""
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        traces = [f"{i:032x}" for i in range(1, 41)]
        t = _recent_ms()
        for i, trace in enumerate(traces):
            _arrive(client, trace, t + 100 + i, root=True)
        count = 4
        parts = [{r[0] for r in _scan(client, t, FAR_MS, count, p)} for p in range(count)]
        assert sorted(t for part in parts for t in part) == sorted(traces)
        assert all(parts), "40 traces spread over 4 partitions"
        assert sum(len(p) for p in parts) == len(traces), "no trace in two partitions"
    finally:
        client.close()


def test_the_spans_read_finds_a_trace_by_id_in_every_service_within_the_look_back(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """The assembly read has no service filter: both halves of a trace come back."""
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        client.command(_statement("spans_write.sql"))
        client.command(_late_child_write())
        sql = _statement("spans_by_trace_read.sql")
        trace = "0af7651916cd43dd8448eb211c80319c"
        rows = client.query_rows(sql, parameters={"trace_ids": [trace], "since": 0})
        assert len(rows) == 2
        assert {r[0] for r in rows} == {trace}
        assert sorted(r[2] for r in rows) == ["", "b7ad6b7169203331"], "root and child"
        none = client.query_rows(sql, parameters={"trace_ids": ["f" * 32], "since": 0})
        assert none == []
        future = client.query_rows(sql, parameters={"trace_ids": [trace], "since": FAR_MS})
        assert future == [], "the look-back bound excludes older spans"
    finally:
        client.close()


def _run(
    clickhouse: ClickHouseServer,
    database: str,
    migrations: Sequence[Migration],
    statements: Sequence[tuple[str, str]],
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, migrations)
        for name, sql in statements:
            if name.endswith("_write.sql"):
                client.command(sql, contract.parameters(sql))
            else:
                client.query_rows(sql, parameters=contract.parameters(sql))
    finally:
        client.close()


@pytest.mark.parametrize("direction", ["code N on schema N-1", "code N-1 on schema N"])
def test_n_minus_1(clickhouse: ClickHouseServer, database: str, direction: str) -> None:
    """CLAUDE.md hard rule 4: one schema step apart, either side keeps working."""
    ref = os.environ.get("CONTRACT_BASE_REF", "main")
    base = at_revision(ref, cwd=ROOT)
    if not base:
        pytest.skip(f"no migrations at {ref}: N-1 applies from the next schema change on")
    if direction == "code N on schema N-1":
        _run(clickhouse, database, base, contract.packaged())
    else:
        _run(clickhouse, database, packaged(), contract.at_revision(ref, cwd=ROOT))


def test_the_drop_guard_fails_clearly_and_a_rerun_applies_only_the_refused_drop(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """#41: ClickHouse refuses DROP TABLE above max_table_size_to_drop (error 359). The runner
    names the migration, table, size and limit and points to the runbook; 0012 is not
    recorded (0011 stays applied); lifting the limit and re-running applies 0012 and the
    migrations after it, nothing before."""
    client = clickhouse.client(database)
    try:
        migrations = packaged()
        migrate(client, [m for m in migrations if m.version <= 10])
        client.command(_statement("spans_write.sql"))  # trace_roots now holds a part
        client.command("SET max_table_size_to_drop = 1")  # the query-level setting wins

        with pytest.raises(DropGuardError) as err:
            migrate(client, migrations)
        msg = str(err.value)
        assert "0012_contract_drop_trace_roots.sql" in msg
        assert f"{database}.trace_roots" in msg
        assert "limit 1 B" in msg
        assert "unavailable" not in msg
        assert "size " in msg
        assert "docs/runbooks/clickhouse-drop-guard.md" in msg
        assert "Code: 359" not in msg, "not the raw server error"
        recorded = [r[0] for r in client.query_rows("SELECT version FROM schema_migrations")]
        assert sorted(recorded) == list(range(1, 12)), "0011 applied, 0012 not recorded"
        tables = {
            r[0]
            for r in client.query_rows(
                "SELECT name FROM system.tables WHERE database = currentDatabase()"
            )
        }
        assert "trace_roots" in tables
        assert "trace_roots_from_otel" not in tables

        client.command("SET max_table_size_to_drop = 0")
        assert [m.version for m in migrate(client, migrations)] == [
            m.version for m in migrations if m.version >= 12
        ], "0012 and the migrations after it"
        tables = {
            r[0]
            for r in client.query_rows(
                "SELECT name FROM system.tables WHERE database = currentDatabase()"
            )
        }
        assert "trace_roots" not in tables
    finally:
        client.close()


def test_the_runbook_is_linked_from_the_migration() -> None:
    sql = next(m.sql for m in packaged() if m.version == 12)
    assert "docs/runbooks/clickhouse-drop-guard.md" in sql
    assert (ROOT / "docs" / "runbooks" / "clickhouse-drop-guard.md").is_file()


def _injected_span_write() -> str:
    """spans_write.sql for the span the fault was injected at (aegis.fault.injection.id)."""
    sql = _statement("spans_write.sql")
    last = "'server.port', '11434')"
    assert sql.count(last) == 1
    return sql.replace(
        last, "'server.port', '11434', 'aegis.fault.injection.id', 'inj-contract-1')"
    )


def test_injection_id_expands_injections_for_old_and_new_writers(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """#36, #14: rows written before the column read '', the injector's contract write is
    idempotent per (trace_id, injection_id), and an injection joins the span it was applied at."""
    client = clickhouse.client(database)
    try:
        before = [m for m in packaged() if m.name != INJECTION_ID_MIGRATION]
        assert len(before) == len(packaged()) - 1
        migrate(client, before)
        client.command(INJECTION_WRITE_BEFORE_0013)  # code N-1 on schema N-1
        migrate(client, packaged())

        ids = (
            "SELECT injection_id FROM injections"
            " WHERE trace_id = '0af7651916cd43dd8448eb211c80319c'"
        )
        assert client.query_rows(ids) == [("",)]
        write = _statement("injections_write.sql")
        client.command(write, contract.parameters(write))  # code N
        client.command(write, contract.parameters(write))  # re-recorded: nothing new
        assert sorted(r[0] for r in client.query_rows(ids)) == ["", "inj-contract-1"]

        client.command(_injected_span_write())
        read = _statement("injections_spans_read.sql")
        rows = client.query_rows(read, contract.parameters(read))
        assert [(r[0], r[1], bytes(r[2]).decode()) for r in rows] == [
            ("inj-contract-1", "context_truncation", "b7ad6b7169203331")
        ]
    finally:
        client.close()
