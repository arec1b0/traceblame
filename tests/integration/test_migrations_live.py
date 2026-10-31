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

# Start value of the trace-assembler's grace (ADR-0003), as in contract/trace_roots_read.sql.
WATERMARK_GRACE_S = 5

ENGINES = {
    "spans": "MergeTree",
    "otel_traces_ingest": "Null",
    "spans_from_otel": "MaterializedView",
    "trace_roots": "MergeTree",
    "trace_roots_from_otel": "MaterializedView",
    "findings": "ReplacingMergeTree",
    "blame": "ReplacingMergeTree",
    "injections": "MergeTree",
    "schema_migrations": "MergeTree",
}


def test_migrate_creates_every_table_once(clickhouse: ClickHouseServer, database: str) -> None:
    client = clickhouse.client(database)
    try:
        assert [m.version for m in migrate(client, packaged())] == [1, 2, 3, 4, 5, 6, 7, 8]
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
        # The watermark scan sees a root only once it is older than the grace (ADR-0003).
        deadline = time.monotonic() + WATERMARK_GRACE_S + 10
        while not results["trace_roots_read.sql"] and time.monotonic() < deadline:
            time.sleep(0.5)
            results["trace_roots_read.sql"] = client.query_rows(_statement("trace_roots_read.sql"))
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
        (root,) = results["trace_roots_read.sql"]
        assert root[1] == b"0af7651916cd43dd8448eb211c80319c"
        assert root[2] == b"b7ad6b7169203331"
    finally:
        client.close()


def _statement(name: str) -> str:
    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql


def _index_section(plan: str, index: str) -> str:
    """The lines of one index (Min-Max, Partition, PrimaryKey) in EXPLAIN indexes = 1."""
    assert index in plan, plan
    return plan.split(index, 1)[1].split("Granules", 1)[0]


def test_the_watermark_query_reads_by_primary_key(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """The assembler's scan (ADR-0003) prunes partitions and granules on ingested_at."""
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        for name, statement in contract.packaged():
            if name.endswith("_write.sql"):
                client.command(statement)  # an empty table has no parts to read an index of
        sql = _statement("trace_roots_read.sql")
        plan = "\n".join(str(r[0]) for r in client.query_rows(f"EXPLAIN indexes = 1 {sql}"))
        primary = _index_section(plan, "PrimaryKey")
        assert "ingested_at" in primary, plan
        assert "Condition: true" not in primary, plan
        assert "ingested_at" in _index_section(plan, "Min-Max"), plan
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
