"""Trace views from ClickHouse, through the storage contract's reads (#15)."""

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from traceblame_detectors.sql_statements import statement
from traceblame_detectors.view import SPAN_COLUMNS, SUMMARY_COLUMNS, TraceView, build_view

SUMMARY_READ = "trace_summary_read.sql"
SPANS_READ = "spans_trace_view_read.sql"
# Spans may start a little before the root's recorded start (clock skew between services); the
# look-back only prunes partitions. Operational, not a detector param.
DEFAULT_LOOKBACK_MS = 3_600_000


class QueryClient(Protocol):
    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]: ...


class ClickHouseTraceSource:
    def __init__(self, client: QueryClient, lookback_ms: int = DEFAULT_LOOKBACK_MS) -> None:
        self._client = client
        self._lookback_ms = lookback_ms
        self._summary_sql = statement(SUMMARY_READ)
        self._spans_sql = statement(SPANS_READ)

    def summary(self, trace_id: str, assembler_version: str) -> dict[str, Any] | None:
        rows = self._client.query_rows(
            self._summary_sql, {"trace_id": trace_id, "assembler_version": assembler_version}
        )
        return dict(zip(SUMMARY_COLUMNS, rows[0], strict=True)) if rows else None

    def span_rows(
        self, trace_id: str, since_ms: int, attr_keys: Iterable[str]
    ) -> list[dict[str, Any]]:
        rows = self._client.query_rows(
            self._spans_sql,
            {"trace_id": trace_id, "since_ms": since_ms, "attr_keys": sorted(attr_keys)},
        )
        return [dict(zip(SPAN_COLUMNS, r, strict=True)) for r in rows]

    def load(
        self, trace_id: str, assembler_version: str, attr_keys: Iterable[str]
    ) -> TraceView | None:
        keys = sorted(attr_keys)
        summary = self.summary(trace_id, assembler_version)
        if summary is None:
            return None
        since_ms = int(summary["start_us"]) // 1000 - self._lookback_ms
        return build_view(summary, self.span_rows(trace_id, since_ms, keys), keys)
