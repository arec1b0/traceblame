"""The migrations against a real ClickHouse: tables, idempotency, checksums, N-1."""

import os
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from traceblame_storage import contract
from traceblame_storage.migrations import Migration, at_revision, packaged
from traceblame_storage.runner import ChecksumMismatchError, migrate
from traceblame_storage.testing import ClickHouseServer

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration

# The trace-assembler's watermark scan (ADR-0003). It reads trace_arrivals, new in this schema,
# so it is not a contract statement yet: code N must run on schema N-1. The assembler (#35)
# moves it into contract/.
SCAN = (Path(__file__).parent / "trace_arrivals_scan.sql").read_text(encoding="utf-8")
# Its grace (start value) and its lower bound, the watermark.
WATERMARK_GRACE_S = 5
SCAN_WATERMARK = "toDateTime64('2026-10-30 23:59:00.000', 3)"

ENGINES = {
    "spans": "MergeTree",
    "otel_traces_ingest": "Null",
    "spans_from_otel": "MaterializedView",
    "trace_roots": "MergeTree",
    "trace_roots_from_otel": "MaterializedView",
    "trace_arrivals": "MergeTree",
    "trace_arrivals_from_otel": "MaterializedView",
    "findings": "ReplacingMergeTree",
    "blame": "ReplacingMergeTree",
    "injections": "MergeTree",
    "schema_migrations": "MergeTree",
}


def test_migrate_creates_every_table_once(clickhouse: ClickHouseServer, database: str) -> None:
    client = clickhouse.client(database)
    try:
        assert [m.version for m in migrate(client, packaged())] == list(range(1, 11))
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
                client.command(sql)
            else:
                results[name] = client.query_rows(sql)
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
    finally:
        client.close()


def _statement(name: str) -> str:
    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql


def _index_section(plan: str, index: str) -> str:
    """The lines of one index (Min-Max, Partition, PrimaryKey) in EXPLAIN indexes = 1."""
    assert index in plan, plan
    return plan.split(index, 1)[1].split("Granules", 1)[0]


def _scan_sql(watermark: str) -> str:
    """The watermark scan (trace_arrivals_scan.sql), after another watermark."""
    assert SCAN.count(SCAN_WATERMARK) == 1
    return SCAN.replace(SCAN_WATERMARK, watermark)


def _scan(client: Any, watermark: str) -> list[tuple[Any, ...]]:
    """The scan once its window holds rows: arrivals show only once older than the grace."""
    deadline = time.monotonic() + WATERMARK_GRACE_S + 10
    rows = client.query_rows(_scan_sql(watermark))
    while not rows and time.monotonic() < deadline:
        time.sleep(0.5)
        rows = client.query_rows(_scan_sql(watermark))
    return rows


def _late_child_write() -> str:
    """spans_write.sql as a child of its own span, exported in a later insert."""
    sql = _statement("spans_write.sql")
    root = "'b7ad6b7169203331', ''"
    assert sql.count(root) == 1
    return sql.replace(root, "'c0ffee00c0ffee00', 'b7ad6b7169203331'")


def test_the_watermark_scan_finds_root_and_late_arrivals_by_primary_key(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """ADR-0003: one scan over (watermark, now - grace]; a root arrival is assembled, a later
    non-root arrival re-assembles; the scan prunes on ingested_at, never a full scan."""
    trace = b"0af7651916cd43dd8448eb211c80319c"
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        client.command(_statement("spans_write.sql"))
        (first,) = _scan(client, SCAN_WATERMARK)
        assert first[:4] == (trace, True, ["contract-check"], ["contract"]), "root: assemble"
        watermark = f"toDateTime64('{first[4].isoformat(sep=' ', timespec='milliseconds')}', 3)"

        client.command(_late_child_write())
        (late,) = _scan(client, watermark)
        assert late[:2] == (trace, False), (
            "late non-root arrival of an assembled trace: re-assemble"
        )
        assert late[4] > first[4], "a later revision"
        (whole,) = _scan(client, SCAN_WATERMARK)
        assert whole[:2] == (trace, True)

        plan = "\n".join(
            str(r[0]) for r in client.query_rows(f"EXPLAIN indexes = 1 {_scan_sql(watermark)}")
        )
        minmax = _index_section(plan, "Min-Max")
        assert "ingested_at" in minmax, plan
        assert "Parts: 1/2" in minmax, "the root's part is before the watermark: pruned"
        primary = _index_section(plan, "PrimaryKey")
        assert "ingested_at" in primary, plan
        assert "Condition: true" not in primary, plan
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
                client.command(sql)
            else:
                client.query_rows(sql)
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
