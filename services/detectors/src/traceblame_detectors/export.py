"""Snapshot export (#15): the spans and summaries of given traces, through the same contract
reads the online path uses, into the snapshot format (sources/snapshot.py).

spans and trace_summary expire 30 days after a trace's start (migrations 0001, 0014): a
dataset must be exported before then. attrs are exported filtered to the declarable keys
(policy.py), so no eval or fault marker, exception text or content is ever written; nothing
is read from injections or eval_traces.
"""

import json
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from traceblame_detectors.canonical import sha256_hex
from traceblame_detectors.policy import DECLARABLE
from traceblame_detectors.sources.clickhouse import SPANS_READ, SUMMARY_READ, ClickHouseTraceSource
from traceblame_detectors.sources.snapshot import FORMAT, MANIFEST, SPANS_FILE, SUMMARY_FILE
from traceblame_detectors.sql_statements import statement

TTL_NOTE = "spans and trace_summary expire 30 days after the trace's start_ts"
_CH_MIGRATIONS = "SELECT version, name, toString(sha256) FROM schema_migrations ORDER BY version"
_PG_MIGRATIONS = "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"


class QueryClient(Protocol):
    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]: ...


class MissingTraceError(LookupError):
    pass


def _git(args: Sequence[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["git", *args],  # noqa: S607 - git from PATH, as every repo script
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except OSError:
        return None
    return out.stdout if out.returncode == 0 else None


def provenance(repo: Path) -> dict[str, Any]:
    head = _git(["rev-parse", "HEAD"], repo)
    status = _git(["status", "--porcelain", "--untracked-files=no"], repo)
    return {
        "commit": None if head is None else head.strip(),
        "dirty": None if status is None else bool(status.strip()),
    }


def _jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def export_snapshot(
    client: QueryClient,
    trace_ids: Sequence[str],
    assembler_version: str,
    out: Path,
    *,
    repo: Path,
    postgres_migrations: Sequence[tuple[Any, ...]] | None = None,
) -> dict[str, Any]:
    """Write the snapshot of these traces into `out` (created, must be empty) and return its
    manifest. Every trace must have a summary of this assembler version."""
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    source = ClickHouseTraceSource(client)
    keys = sorted(DECLARABLE)
    summaries: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for trace_id in sorted(set(trace_ids)):
        summary = source.summary(trace_id, assembler_version)
        if summary is None:
            raise MissingTraceError(
                f"{trace_id}: no trace_summary of assembler {assembler_version}"
            )
        summaries.append(summary)
        since_ms = int(summary["start_us"]) // 1000 - 3_600_000
        for row in source.span_rows(trace_id, since_ms, keys):
            spans.append({"trace_id": trace_id, **_plain(row)})
    _jsonl(out / SUMMARY_FILE, [_plain(s) for s in summaries])
    _jsonl(out / SPANS_FILE, spans)
    files = {name: sha256_hex((out / name).read_bytes()) for name in (SUMMARY_FILE, SPANS_FILE)}
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **provenance(repo),
        "clickhouse_migrations": [
            {"version": int(v), "name": str(n), "sha256": str(s)}
            for v, n, s in client.query_rows(_CH_MIGRATIONS)
        ],
        "postgres_migrations": None
        if postgres_migrations is None
        else [
            {"version": int(v), "name": str(n), "sha256": str(s)} for v, n, s in postgres_migrations
        ],
        "profile_versions": sorted({str(s["profile_version"]) for s in spans}),
        "assembler_versions": sorted({str(s["assembler_version"]) for s in summaries}),
        "contract_reads": {
            name: sha256_hex(statement(name).encode("utf-8")) for name in (SUMMARY_READ, SPANS_READ)
        },
        "attr_keys": keys,
        "trace_count": len(summaries),
        "span_count": len(spans),
        "ttl_note": TTL_NOTE,
        "files": files,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
    return manifest


def postgres_migrations(url: str) -> list[tuple[Any, ...]]:
    from traceblame_storage.client import connect_postgres  # only when asked for

    with connect_postgres(url) as conn:
        return [tuple(r) for r in conn.execute(_PG_MIGRATIONS).fetchall()]


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe values: tuples and maps as lists and dicts."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, tuple | list):
            out[k] = list(cast("Sequence[Any]", v))
        elif isinstance(v, dict):
            out[k] = {str(a): str(b) for a, b in cast("Mapping[Any, Any]", v).items()}
        else:
            out[k] = v
    return out
