"""The trace-assembler's ClickHouse statements and trace_summary, on a real server (#35)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from traceblame_storage import contract
from traceblame_storage.migrations import packaged
from traceblame_storage.runner import migrate
from traceblame_storage.testing import ClickHouseServer

pytestmark = pytest.mark.integration

HERE = Path(__file__).parent
ASSEMBLED = (HERE / "trace_summary_assembled_read.sql").read_text(encoding="utf-8")
RECONCILE = (HERE / "trace_summary_reconcile_read.sql").read_text(encoding="utf-8")
TRACE = "0af7651916cd43dd8448eb211c80319c"
HOUR_MS = 3_600_000
# Rows older than their TTL (30 days for trace_summary, 2 days for trace_arrivals) are dropped
# by ClickHouse at insert, so every stamp here is relative to an hour ago.
T0_MS = int(datetime.now(UTC).timestamp() * 1000) - 3 * HOUR_MS


def _statement(name: str) -> str:
    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql


def _at(ms: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)


def _summary(trace: str, revision_ms: int, *, version: str = "1.0.0", spans: int = 1) -> list[Any]:
    return [
        trace, version, _at(revision_ms), "svc", "tenant", "00f067aa0ba902b7", "Ok",
        _at(T0_MS), 12.5, spans, 0, "sha256:b", "s-1",
    ]  # fmt: skip


SUMMARY_COLUMNS = [
    "trace_id", "assembler_version", "revision", "service", "tenant_id", "root_span_id",
    "root_status_code", "start_ts", "duration_ms", "span_count", "error_span_count",
    "bundle_digest", "session_id",
]  # fmt: skip


def _write_summary(client: Any, *rows: list[Any]) -> None:
    client.insert("trace_summary", list(rows), SUMMARY_COLUMNS)


def _arrive(client: Any, trace: str, at_ms: int, *, root: bool) -> None:
    client.insert(
        "trace_arrivals",
        [[trace, _at(at_ms), root, "svc", "tenant"]],
        ["trace_id", "ingested_at", "is_root", "service", "tenant_id"],
    )


def test_an_identical_reassembly_is_one_row_and_a_newer_revision_supersedes_it(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        _write_summary(client, _summary(TRACE, T0_MS + 5_000, spans=2))
        _write_summary(client, _summary(TRACE, T0_MS + 5_000, spans=2))  # the replayed window
        client.command("OPTIMIZE TABLE trace_summary FINAL")
        assert client.query_rows("SELECT count() FROM trace_summary") == [(1,)]

        _write_summary(client, _summary(TRACE, T0_MS + 9_000, spans=3))  # a late span
        _write_summary(
            client, _summary(TRACE, T0_MS + 7_000, spans=9)
        )  # an older revision, written late
        client.command("OPTIMIZE TABLE trace_summary FINAL")
        rows = client.query_rows(
            "SELECT toUnixTimestamp64Milli(revision), span_count FROM trace_summary"
        )
        assert rows == [(T0_MS + 9_000, 3)], "the highest revision wins, whatever the write order"

        _write_summary(client, _summary(TRACE, T0_MS + 9_500, version="1.1.0"))
        client.command("OPTIMIZE TABLE trace_summary FINAL")
        assert client.query_rows("SELECT count() FROM trace_summary") == [(2,)], (
            "one row per version"
        )
    finally:
        client.close()


def test_the_assembled_read_gives_the_newest_revision_of_any_version(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        other = "b" * 32
        _write_summary(
            client,
            _summary(TRACE, T0_MS + 5_000),
            _summary(TRACE, T0_MS + 8_000, version="2.0.0"),
            _summary(other, T0_MS + 1_000),
        )
        ids = [TRACE, other, "c" * 32]
        rows = client.query_rows(ASSEMBLED, parameters={"trace_ids": ids})
        assert dict(rows) == {TRACE: T0_MS + 8_000, other: T0_MS + 1_000}, (
            "a trace without a summary is absent"
        )
    finally:
        client.close()


def test_the_reconciliation_counts_misses_and_orphans_of_a_closed_window(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        h0, h1 = T0_MS - T0_MS % HOUR_MS, T0_MS - T0_MS % HOUR_MS + HOUR_MS
        done, missing, orphan, late_root = "1" * 32, "2" * 32, "3" * 32, "4" * 32
        _arrive(client, done, h0 + 10, root=True)
        _write_summary(client, _summary(done, h0 + 10))
        _arrive(client, missing, h0 + 20, root=True)  # a root arrival, no summary
        _arrive(client, orphan, h0 + 30, root=False)  # spans, no root, no summary
        _arrive(client, late_root, h0 + 40, root=False)  # its root arrives in the next hour
        _arrive(client, late_root, h1 + 5, root=True)
        _arrive(client, "5" * 32, h1 + 6, root=True)  # outside the window
        params = {"h0": h0, "h1": h1, "since": h0 - HOUR_MS}
        assert client.query_rows(RECONCILE, parameters=params) == [(1, 1)]

        _write_summary(client, _summary(missing, h0 + 20))
        assert client.query_rows(RECONCILE, parameters=params) == [(0, 1)], "the miss is repaired"
    finally:
        client.close()


def test_the_spans_read_uses_the_trace_id_bloom_filter_and_prunes_partitions(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """ADR-0003 amendment: no service filter, so the cost rests on the skip index and on the
    start_ts bound that prunes daily partitions."""
    client = clickhouse.client(database)
    try:
        migrate(client, packaged())
        client.command(_statement("spans_write.sql"))
        # Filler parts so the index has something to skip.
        for i in range(1, 9):
            day = _at(1_700_000_000_000 + i * 86_400_000)
            client.insert(
                "spans",
                [[f"{i:032x}", f"{i:016x}", "", "svc", "op", "llm", day, 1.0, "Ok"]],
                ["trace_id", "span_id", "parent_span_id", "service", "operation", "span_kind",
                 "start_ts", "duration_ms", "status_code"],
            )  # fmt: skip
        sql = _statement("spans_by_trace_read.sql")
        since = 1_700_000_000_000 + 5 * 86_400_000
        plan = "\n".join(
            str(r[0])
            for r in client.query_rows(
                f"EXPLAIN indexes = 1 {sql}", parameters={"trace_ids": [TRACE], "since": since}
            )
        )
        assert "idx_trace_id" in plan, plan
        assert "Partition" in plan, plan
        # the bound on start_ts keeps the 2026-10-31 span and drops the filler before `since`
        rows = client.query_rows(sql, parameters={"trace_ids": [TRACE], "since": since})
        assert len(rows) == 1
    finally:
        client.close()
