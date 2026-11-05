"""Kind helpers of the fault drivers (#14): ClickHouse over a port-forward, the trace-assembler's
TRACE_COMPLETED stream, the settle wait, the eval tenants. Run with the workspace environment
(`uv run --frozen`); every command that sends traffic to kind runs under the shared lock
(flock /tmp/traceblame-kind.lock), which the caller holds, never these helpers.
"""

import asyncio
import base64
import json
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fixtures"))

import checks  # noqa: E402
import yaml  # noqa: E402
from kindlib import NAMESPACE, PortForward, kubectl, spans_of, stored  # noqa: E402

from traceblame_faults import kinds, ledger, record  # noqa: E402
from traceblame_storage.client import ClickHouseClient, parse_url  # noqa: E402

ROOT = HERE.parents[1]
STREAM = "TRACE_COMPLETED"
EVAL_TENANT = "tenant-eval"
CONFIG = HERE / "validation.yaml"
WORK = ROOT / ".tools" / "faults"


def config() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(CONFIG.read_text(encoding="utf-8")))


def run_dir(run_id: str, app: str) -> Path:
    return WORK / run_id / app


def load_state(d: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((d / "state.json").read_text(encoding="utf-8")))


def save_state(d: Path, state: dict[str, Any]) -> None:
    (d / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def eval_tenants() -> list[str]:
    value = kubectl(
        "-n", NAMESPACE, "get", "configmap", "eval-tenants",
        "-o", "jsonpath={.data.TRACEBLAME_EVAL_TENANTS}",
    )  # fmt: skip
    return cast("list[str]", yaml.safe_load(value) or [])


def require_eval_tenant() -> str:
    tenants = eval_tenants()
    if EVAL_TENANT not in tenants:
        raise SystemExit(f"faults: eval_tenants is {tenants}; want {EVAL_TENANT}")
    return yaml.safe_dump(tenants, default_flow_style=True).strip()


def _secret(name: str, key: str) -> str:
    raw = kubectl("-n", NAMESPACE, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


@contextmanager
def clickhouse_client() -> Generator[ClickHouseClient]:
    """The kind ClickHouse over HTTP, through a port-forward, as the traceblame user."""
    user = _secret("clickhouse-credentials", "user")
    password = _secret("clickhouse-credentials", "password")
    forward = PortForward("svc/clickhouse-traceblame", 8123)
    try:
        url = f"http://{quote(user)}:{quote(password, safe='')}@127.0.0.1:{forward.port}/traceblame"
        client = ClickHouseClient(parse_url(url))
        try:
            yield client
        finally:
            client.close()
    finally:
        forward.close()


async def _nats(fn: Callable[[Any], Awaitable[Any]]) -> Any:
    import nats

    forward = PortForward("svc/nats", 4222)
    try:
        connect = cast("Callable[[str], Awaitable[Any]]", nats.connect)  # pyright: ignore[reportUnknownMemberType]
        nc: Any = await connect(f"nats://127.0.0.1:{forward.port}")
        try:
            return await fn(nc.jetstream())
        finally:
            await nc.close()
    finally:
        forward.close()


def stream_last_seq() -> int:
    async def last(js: Any) -> int:
        info = await js.stream_info(STREAM)
        return int(info.state.last_seq)

    return cast("int", asyncio.run(_nats(last)))


def completed_since(seq: int, trace_ids: Iterable[str]) -> dict[str, int]:
    """trace.completed messages after seq for these traces: trace_id -> message count."""
    wanted = set(trace_ids)

    async def read(js: Any) -> dict[str, int]:
        info = await js.stream_info(STREAM)
        out: dict[str, int] = {}
        for n in range(max(seq + 1, int(info.state.first_seq)), int(info.state.last_seq) + 1):
            msg: Any = await js.get_msg(STREAM, seq=n)
            payload = json.loads(msg.data)
            tid = str(payload["trace_id"])
            if tid in wanted:
                out[tid] = out.get(tid, 0) + 1
        return out

    return cast("dict[str, int]", asyncio.run(_nats(read)))


def settle(
    trace_ids: Iterable[str], sent: int, timeout: float, log: Callable[[str], None]
) -> list[dict[str, Any]]:
    """Poll the stored spans of these traces until all `sent` are there, then 15 s more."""
    ids = sorted(set(trace_ids))
    deadline = time.monotonic() + timeout
    rows: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        rows = stored(ids)
        if len(rows) >= sent:
            break
        log(f"settling: {len(rows)}/{sent} spans stored")
        time.sleep(10)
    time.sleep(15)
    return stored(ids)


def trace_summaries(trace_ids: Iterable[str]) -> dict[str, int]:
    from kindlib import clickhouse

    ids = ",".join(f"'{t}'" for t in sorted(set(trace_ids)) if len(t) == 32 and t.isalnum())
    if not ids:
        return {}
    rows = clickhouse(
        f"SELECT trace_id, span_count FROM trace_summary FINAL WHERE trace_id IN ({ids})"  # noqa: S608 - hex ids
    )
    return {str(r["trace_id"]): int(r["span_count"]) for r in rows}


def wait_assembled(
    trace_ids: Iterable[str], timeout: float, log: Callable[[str], None]
) -> dict[str, int]:
    ids = set(trace_ids)
    deadline = time.monotonic() + timeout
    found: dict[str, int] = {}
    while time.monotonic() < deadline:
        found = trace_summaries(ids)
        if set(found) >= ids:
            break
        log(f"assembling: {len(found)}/{len(ids)} traces in trace_summary")
        time.sleep(10)
    return found


_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TABLE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
# The run's own files that feed the report (never the content needles themselves).
RUN_FILES = ("otlp.json", "ledger.jsonl", "outcomes.jsonl", "plan.jsonl", "queries.jsonl",
             "state.json")  # fmt: skip
Content = Callable[[dict[str, list[dict[str, Any]]], list[Path]], dict[str, Any]]
# The dataset's own question / task ids (#14 decision): a runbook task id is also its sandbox
# workload's name, which the #13 needles hold as a label value, so the id would match itself.
# Only these fields are left out of the content scan; every other column and key is scanned.
ID_FIELDS = frozenset({"query_ref", "ref"})


def strip_ids(value: Any) -> tuple[Any, int]:
    """The value without ID_FIELDS keys at any depth, and how many it dropped."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        dropped = 0
        for k, v in cast("dict[str, Any]", value).items():
            if k in ID_FIELDS:
                dropped += 1
                continue
            out[k], n = strip_ids(v)
            dropped += n
        return out, dropped
    if isinstance(value, list):
        items = [strip_ids(v) for v in cast("list[Any]", value)]
        return [v for v, _ in items], sum(n for _, n in items)
    return value, 0


def json_docs(path: Path) -> list[Any]:
    """A JSON file, or a JSON-lines file, as parsed documents (the content check reads parsed
    strings, so a needle with a newline or a quote matches its JSON-escaped occurrence)."""
    text = path.read_text(encoding="utf-8")
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    docs: list[Any] = []
    for line in text.splitlines():
        try:
            docs.append(json.loads(line))
        except ValueError:
            docs.append(line)  # a non-JSON line is scanned as it is
    return docs


def run_tables() -> list[str]:
    """Every table of the traceblame database with a trace_id column (views excluded)."""
    from kindlib import clickhouse

    rows = clickhouse(
        "SELECT DISTINCT c.table AS name FROM system.columns AS c"
        " INNER JOIN system.tables AS t ON c.database = t.database AND c.table = t.name"
        " WHERE c.database = currentDatabase() AND c.name = 'trace_id'"
        " AND t.engine NOT LIKE '%View' ORDER BY name"
    )
    return [str(r["name"]) for r in rows if _TABLE.fullmatch(str(r["name"]))]


def run_rows(trace_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Every stored row of these traces in every such table, without FINAL: versions that a
    merge has not collapsed yet are scanned too."""
    from kindlib import clickhouse

    ids = ",".join(f"'{t}'" for t in sorted(set(trace_ids)) if _HEX32.fullmatch(t))
    if not ids:
        return {}
    # Table names come from system.columns and match _TABLE; ids are checked hex.
    return {
        t: clickhouse(f"SELECT * FROM `{t}` WHERE toString(trace_id) IN ({ids})")  # noqa: S608
        for t in run_tables()
    }


def content_scan(
    d: Path, records: list[ledger.Record], content: Content, log: Callable[[str], None]
) -> dict[str, Any]:
    """The content check over every stored row of the run in every table and over the run's
    files (parsed copies under content-scan/), all without the ID_FIELDS; the counts scanned go
    into the result next to the app's needle counts."""
    log("content check: every stored row of the run")
    tables = run_rows({r.trace_id for r in records})
    stripped, dropped = strip_ids(tables)
    files = [d / name for name in RUN_FILES if (d / name).is_file()]
    scan = d / "content-scan"
    scan.mkdir(exist_ok=True)
    copies: list[Path] = []
    for f in files:
        docs, n = strip_ids(json_docs(f))
        dropped += n
        copy = scan / f"{f.name}.json"
        copy.write_text(json.dumps(docs), encoding="utf-8")
        copies.append(copy)
    out = content(cast("dict[str, list[dict[str, Any]]]", stripped), copies)
    otlp = json.loads((d / "otlp.json").read_text(encoding="utf-8"))
    out["scanned"] = {
        "rows": {t: len(rows) for t, rows in tables.items()},
        "rows_total": sum(len(rows) for rows in tables.values()),
        "exported_spans": len(spans_of(otlp)),
        "files": {f.name: len(f.read_text(encoding="utf-8").splitlines()) for f in files},
        "id_fields_excluded": {"fields": sorted(ID_FIELDS), "values": dropped},
    }
    return out


def recheck_content(d: Path, content: Content, log: Callable[[str], None]) -> dict[str, Any]:
    """Only the content check of a finished run, over what is stored now (read-only; the
    caller holds the kind lock): replaces check.json's content_check and its content problem."""
    check = cast("dict[str, Any]", json.loads((d / "check.json").read_text(encoding="utf-8")))
    result = content_scan(d, ledger.read(d / "ledger.jsonl"), content, log)
    check["content_check"] = result
    check["problems"] = [p for p in check["problems"] if not p.startswith("content found")]
    if result.get("leaking"):
        check["problems"].append(f"content found: {result}")
    (d / "check.json").write_text(json.dumps(check, indent=2) + "\n", encoding="utf-8")
    return check


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _tid(value: Any) -> str:
    return bytes(value).decode() if isinstance(value, bytes | bytearray) else str(value)


def check_run(
    app: str,
    d: Path,
    state: dict[str, Any],
    cfg: dict[str, Any],
    *,
    golden: Callable[[str], Iterable[str]],
    content: Content,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Per trace and per kind: stored spans, markers on the intended span kind, the join, the
    signal against the clean trace of the same query, assembly, content. Writes check.json."""
    problems: list[str] = []
    records = ledger.read(d / "ledger.jsonl")
    outcomes = [
        json.loads(line)
        for line in (d / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.startswith("{")
    ]
    otlp = json.loads((d / "otlp.json").read_text(encoding="utf-8"))
    sent = {(s["traceId"], s["spanId"]) for _, s in spans_of(otlp)}
    traces = {r.trace_id for r in records}
    rows = settle(traces, len(sent), float(cfg["settle_seconds"]), log)
    got = {(str(r["trace_id"]), str(r["span_id"])) for r in rows}
    if got != sent:
        problems.append(
            f"{len(sent)} spans sent, {len(got)} stored; {len(sent - got)} missing, "
            f"{len(got - sent)} unexpected"
        )
    if any(r["tenant_id"] != EVAL_TENANT for r in rows):
        problems.append("a stored span of another tenant")
    if any(r["resource_attrs"].get("aegis.eval.run.id") != state["run_id"] for r in rows):
        problems.append("a stored span without the eval run marker")
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_trace.setdefault(str(r["trace_id"]), []).append(r)
    summaries = wait_assembled(traces, float(cfg["assemble_seconds"]), log)
    completed = completed_since(int(state["run"]["nats_seq_before"]), traces)
    with clickhouse_client() as client:
        stored_eval = {
            _tid(r[0]): r
            for r in client.query_rows(
                record.statement("eval_traces_read.sql"), {"eval_run_id": state["run_id"]}
            )
        }
        join_sql = record.statement("injections_spans_read.sql")
        joins = {t: client.query_rows(join_sql, {"trace_id": t}) for t in traces}
        injection_params = {
            str(r[0]): str(r[1])
            for r in client.query_rows(
                "SELECT injection_id, params FROM injections"
                " WHERE has({ids:Array(String)}, toString(trace_id))",
                {"ids": sorted(traces)},
            )
        }
    clean_by_query = {r.query_ref: r for r in records if ledger.status(r) == "clean"}
    # Without a clean run of the same query (runbook tasks run once each), the first clean
    # trace of the same stratum is the comparison.
    clean_by_stratum: dict[str, ledger.Record] = {}
    for r in records:
        if ledger.status(r) == "clean":
            clean_by_stratum.setdefault(r.stratum, r)
    out_by_trace = {o.get("trace_id"): o for o in outcomes}
    per_trace: list[dict[str, Any]] = []
    for rec in records:
        t_rows = by_trace.get(rec.trace_id, [])
        golden_ids = tuple(golden(rec.query_ref))
        feats = checks.features(t_rows, golden_ids)
        clean_rec = clean_by_query.get(rec.query_ref) or clean_by_stratum.get(rec.stratum)
        clean_feats = (
            checks.features(by_trace.get(clean_rec.trace_id, []), golden_ids) if clean_rec else None
        )
        status = ledger.status(rec)
        outcome = out_by_trace.get(rec.trace_id, {})
        entry: dict[str, Any] = {
            "trace_id": rec.trace_id,
            "query": ids_hash(rec.query_ref),
            "stratum": rec.stratum,
            "kind": rec.planned_fault_kind or "clean",
            "status": status,
            "spans_stored": len(t_rows),
            "trace_summary": rec.trace_id in summaries,
            "trace_completed": rec.trace_id in completed,
            "eval_traces_row": rec.trace_id in stored_eval,
            "seconds": outcome.get("seconds"),
            "outcome": outcome.get("outcome"),
            "steps": outcome.get("steps"),
            "bundle_digest": sorted(feats.digests),
            "golden_in_topk": feats.golden_in_topk,
            "max_score": feats.max_score,
            "loop_max": feats.loop_max,
            "host_load1": rec.host_load1,
        }
        ev = stored_eval.get(rec.trace_id)
        if ev is None or str(ev[9]) != rec.injection_id or str(ev[8]) != rec.planned_fault_kind:
            problems.append(f"{rec.trace_id}: eval_traces row missing or different")
        if rec.bundle_digest and feats.digests != frozenset({rec.bundle_digest}):
            problems.append(f"{rec.trace_id}: spans carry {sorted(feats.digests)}, not its digest")
        if status == "applied":
            kind = kinds.KINDS[rec.fault_kind]
            ok, seen = checks.marker_kinds_ok(t_rows, rec.injection_id, kind.marker[app])
            joined = [r for r in joins.get(rec.trace_id, []) if str(r[0]) == rec.injection_id]
            sig = checks.signal(
                rec.fault_kind,
                t_rows,
                feats,
                clean_feats,
                injection_id=rec.injection_id,
                bundle_digest=rec.params.get("bundle_digest"),
                repeat_min=int(cfg["loop"]["repeat_min"]),
                mode=rec.params.get("mode"),
            )
            params_ok = True
            try:
                kinds.check_params(
                    rec.fault_kind,
                    json.loads(injection_params.get(rec.injection_id, "{}")),
                    applied=True,
                )
            except (kinds.ParamsError, ValueError):
                params_ok = False
                problems.append(f"{rec.trace_id}: injections.params missing or not closed-schema")
            entry |= {
                "marker_spans": seen,
                "marker_kind_ok": ok,
                "marker_spans_set": outcome.get("marked_spans"),
                "joined_spans": len(joined),
                "params_ok": params_ok,
                "signal": sig,
            }
            if not ok:
                problems.append(f"{rec.trace_id} {rec.fault_kind}: marker on {seen}")
            if outcome.get("marked_spans") is not None and len(seen) != outcome["marked_spans"]:
                problems.append(
                    f"{rec.trace_id}: {outcome['marked_spans']} spans marked, {len(seen)} stored"
                )
            if not joined:
                problems.append(f"{rec.trace_id}: the injection does not join its span")
        elif feats.marked:
            problems.append(f"{rec.trace_id}: a {status} trace carries a marker")
        if not entry["trace_summary"] or not entry["trace_completed"]:
            problems.append(f"{rec.trace_id}: not assembled (trace_summary / trace.completed)")
        per_trace.append(entry)
    no_trace = [o for o in outcomes if o.get("status") == "no_trace"]
    if no_trace:
        problems.append(f"{len(no_trace)} entries produced no trace")

    per_kind: dict[str, Any] = {}
    for kind in ["clean", *sorted(k for k, v in kinds.KINDS.items() if app in v.apps)]:
        items = [e for e in per_trace if e["kind"] == kind]
        applied = [e for e in items if e["status"] == "applied"]
        seconds = [float(e["seconds"]) for e in items if e["seconds"] is not None]
        per_kind[kind] = {
            "component": kinds.KINDS[kind].component if kind in kinds.KINDS else "",
            "traces": len(items),
            "applied": len(applied),
            "not_reached": sum(e["status"] == "not_reached" for e in items),
            "marker_ok": sum(bool(e.get("marker_kind_ok")) for e in applied),
            "joined": sum(e.get("joined_spans", 0) > 0 for e in applied),
            "moved": sum(bool(e.get("signal", {}).get("moved")) for e in applied),
            "assembled": sum(bool(e["trace_summary"] and e["trace_completed"]) for e in items),
            "seconds_median": round(statistics.median(seconds), 1) if seconds else None,
            "seconds_p95": _percentile(seconds, 0.95),
            "host_load1_median": _median(
                [float(e["host_load1"]) for e in items if e.get("host_load1") is not None]
            ),
            "outcomes": dict(Counter(str(e["outcome"]) for e in items)),
        }
    result: dict[str, Any] = {
        "app": app,
        "run_id": state["run_id"],
        "baseline_digest": next(
            (r.params.get("baseline_digest") for r in records if r.params.get("baseline_digest")),
            None,
        ),
        "spans": {"sent": len(sent), "stored": len(got)},
        "per_kind": per_kind,
        "per_trace": per_trace,
        "problems": problems,
    }
    result["content_check"] = content_scan(d, records, content, log)
    if result["content_check"].get("leaking"):
        problems.append(f"content found: {result['content_check']}")
    (d / "check.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def ids_hash(query_ref: str) -> str:
    from traceblame_faults import ids

    return ids.query_hash(query_ref)
