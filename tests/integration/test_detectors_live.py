"""The detectors against a real ClickHouse (#15): their migrations and statements, supersession
by revision, and the golden fixtures through the built collector and trace-assembler into the
online handler and, exported as a snapshot, into the offline runner. No NATS consumer here (the
worker is PR-B); the assembler publishes to a JetStream test container as usual."""

import json
import re
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pytest

from traceblame_contracts.fixtures import discover_fixtures
from traceblame_detectors.api import Detector
from traceblame_detectors.cli import main as detectors_cli
from traceblame_detectors.config import DetectorConfig, load_config
from traceblame_detectors.context import StaticBaselineProvider
from traceblame_detectors.engine import DetectorRun, Providers, RunStatus, run_trace
from traceblame_detectors.model import ServiceBaseline
from traceblame_detectors.online import Outcome, handle
from traceblame_detectors.policy import DECLARABLE
from traceblame_detectors.sinks import ClickHouseSink
from traceblame_detectors.sources.clickhouse import ClickHouseTraceSource
from traceblame_detectors.testing import TRACE_ID, load_detector, span_rows, summary_row
from traceblame_detectors.view import TraceView, build_view
from traceblame_storage import contract
from traceblame_storage.client import connect_postgres
from traceblame_storage.migrations import POSTGRES, packaged
from traceblame_storage.migrations import packaged as clickhouse_migrations
from traceblame_storage.pg_runner import migrate as pg_migrate
from traceblame_storage.runner import migrate
from traceblame_storage.testing import ClickHouseServer, NatsServer

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FIXTURES = discover_fixtures(ROOT / "collector" / "testdata" / "fixtures")
PLUGINS = ROOT / "services" / "detectors" / "tests" / "plugins"
ASSEMBLER_VERSION = "1.0.0"
FINDINGS_MIGRATION = "findings_revision"
# A string in a trace view that names a label or an injection (rule 9, the value scan).
LABEL_LIKE = re.compile(r"inject|fault|aegis", re.IGNORECASE)
# Matches that are not labels, by field and exact value shape: the profile's own name.
NOT_LABELS = {"profile_version": re.compile(r"^aegis-telemetry/\d+$")}

COLLECTOR_CONFIG = """
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{otlp_port}
processors:
  normalizer:
    tenant_id: integration
    eval_tenants: [integration, tenant-eval]
exporters:
  clickhouse:
    endpoint: tcp://{host}:{native_port}?dial_timeout=10s
    database: {database}
    username: {user}
    password: {password}
    create_schema: false
    traces_table_name: otel_traces_ingest
    async_insert: false
    timeout: 10s
service:
  telemetry:
    metrics:
      level: none
    logs:
      level: warn
  pipelines:
    traces:
      receivers: [otlp]
      processors: [normalizer]
      exporters: [clickhouse]
"""


_SORTING_KEY = (
    "SELECT sorting_key FROM system.tables WHERE database = {db:String} AND name = 'findings'"
)


def _statement(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _wait(what: str, check: Callable[[], bool], timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not happen within {timeout:.0f} s")
        time.sleep(0.25)


def _registry() -> dict[str, type[Detector]]:
    return {
        n: load_detector(PLUGINS / n / "detector.py")
        for n in ("reference_root", "reference_baseline")
    }


def _config() -> DetectorConfig:
    return load_config({"detectors": {"reference_root": {}, "reference_baseline": {}}}, _registry())


# --- schema and statements -------------------------------------------------------------------


def test_the_detectors_statements_run_on_the_schema(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        names = sorted(p.name for p in HERE.glob("detectors_*.sql"))
        writes = [n for n in names if n.endswith("_write.sql")]
        assert len(names) == 6
        assert len(writes) == 3
        for name in writes:
            sql = _statement(name)
            client.command(sql, contract.parameters(sql))
        reads = {
            n: client.query_rows(_statement(n), contract.parameters(_statement(n)))
            for n in names
            if n.endswith("_read.sql")
        }
        assert [r[:3] for r in reads["detectors_findings_current_read.sql"]] == [
            ("6f1c1f1e-8a43-5f1c-9a4e-0d1f2a3b4c5d", "b7ad6b7169203331", "context_truncation")
        ]
        (run,) = reads["detectors_runs_read.sql"]
        assert run[:3] == ("context_truncation", "0.1.0+p0123abcd", 1793700000123)
        assert run[6] == "ok"
        assert run[8] == 1
        (params,) = reads["detectors_params_read.sql"]
        assert params[4] == '{"min_ratio":0.95}'
    finally:
        client.close()


def test_findings_get_a_revision_in_the_sorting_key_and_old_rows_read_revision_0(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        before = [m for m in clickhouse_migrations() if m.name != FINDINGS_MIGRATION]
        cut = next(m.version for m in clickhouse_migrations() if m.name == FINDINGS_MIGRATION)
        migrate(client, [m for m in before if m.version < cut])
        old_write = next(s for n, s in contract.packaged() if n == "findings_write.sql")
        client.command(old_write)  # code N-1's write, on schema N-1
        migrate(client, clickhouse_migrations())
        (key,) = client.query_rows(_SORTING_KEY, {"db": database})
        assert key[0] == "trace_id, detector, detector_version, span_id, revision, run_digest"
        assert client.query_rows(
            "SELECT toUnixTimestamp64Milli(revision), input_digest FROM findings"
        ) == [(0, "")]
        client.command(old_write)  # code N-1's write still works on schema N
        sql = _statement("detectors_findings_write.sql")
        params = contract.parameters(sql)
        client.command(sql, params)
        client.command(sql, params)  # a redelivery: the same row
        client.command(sql, {**params, "revision_ms": params["revision_ms"] + 1})  # a new revision
        client.command("OPTIMIZE TABLE findings FINAL")
        rows = client.query_rows(
            "SELECT toUnixTimestamp64Milli(revision), count() FROM findings "
            "GROUP BY revision ORDER BY revision"
        )
        assert rows == [(0, 1), (params["revision_ms"], 1), (params["revision_ms"] + 1, 1)]
    finally:
        client.close()


def test_the_findings_migration_is_idempotent_when_rerun_by_hand(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """The runner applies a migration once (schema_migrations). Rerun by hand (a ClickHouse DDL
    that failed half way is rerun, ADR-0004), the ADD COLUMN IF NOT EXISTS clauses are no-ops
    and ClickHouse accepts the unchanged sorting key: the table stays as it was."""
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        (m,) = [m for m in clickhouse_migrations() if m.name == FINDINGS_MIGRATION]
        (statement,) = m.statements()
        client.command(statement)
        (key,) = client.query_rows(_SORTING_KEY, {"db": database})
        assert key[0] == "trace_id, detector, detector_version, span_id, revision, run_digest"
    finally:
        client.close()


def _runs_write(revision_ms: int, finding_count: int, digest: str) -> dict[str, Any]:
    params = contract.parameters(_statement("detectors_runs_write.sql"))
    return {
        **params,
        "revision_ms": revision_ms,
        "finding_counts": [finding_count],
        "input_digests": [digest],
        "run_digests": [digest],
    }


def _findings_write(revision_ms: int, span_ids: Sequence[str], digest: str) -> dict[str, Any]:
    p = contract.parameters(_statement("detectors_findings_write.sql"))
    n = len(span_ids)
    return {
        **p,
        "revision_ms": revision_ms,
        "finding_ids": [str(uuid.uuid4()) for _ in span_ids],
        "span_ids": list(span_ids),
        **{
            k: p[k] * n
            for k in (
                "detectors",
                "detector_versions",
                "components",
                "severities",
                "scores",
                "details",
            )
        },
        "input_digests": [digest] * n,
        "run_digests": [digest] * n,
    }


def test_a_newer_revision_supersedes_whatever_the_insert_order(
    clickhouse: ClickHouseServer, database: str
) -> None:
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        fw, rw = _statement("detectors_findings_write.sql"), _statement("detectors_runs_write.sql")
        current = _statement("detectors_findings_current_read.sql")
        trace = {"trace_id": contract.parameters(current)["trace_id"]}
        a, b = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
        client.command(fw, _findings_write(100, [a, b], "d1"))
        client.command(rw, _runs_write(100, 2, "d1"))
        assert sorted(r[1] for r in client.query_rows(current, trace)) == [a, b]
        # revision 200 finds only span a; its findings land before its run row.
        client.command(fw, _findings_write(200, [a], "d2"))
        assert sorted(r[1] for r in client.query_rows(current, trace)) == [a, b], (
            "not committed yet"
        )
        client.command(rw, _runs_write(200, 1, "d2"))
        assert [(r[1], r[4]) for r in client.query_rows(current, trace)] == [(a, 200)]
        # A late replay of revision 100 (redelivered after 200) changes nothing.
        client.command(fw, _findings_write(100, [a, b], "d1"))
        client.command(rw, _runs_write(100, 2, "d1"))
        assert [(r[1], r[4]) for r in client.query_rows(current, trace)] == [(a, 200)]
    finally:
        client.close()


# --- the pipeline ------------------------------------------------------------------------------


@dataclass
class Pipeline:
    clickhouse: ClickHouseServer
    database: str
    otlp_port: int
    tmp_path: Path
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list[subprocess.Popen[bytes]])

    def ch(self) -> Any:
        return self.clickhouse.client(self.database)

    @property
    def url(self) -> str:
        return self.clickhouse.url(self.database)

    def post(self, body: bytes) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.otlp_port}/v1/traces",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            assert resp.status == 200

    def rows(self, sql: str, params: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
        client = self.ch()
        try:
            return client.query_rows(sql, params)
        finally:
            client.close()

    def summaries(self) -> dict[str, int]:
        """trace id -> newest revision (ms) of its summary."""
        return {
            str(t): int(r)
            for t, r in self.rows(
                "SELECT toString(trace_id), toUnixTimestamp64Milli(max(revision)) "
                "FROM trace_summary GROUP BY trace_id"
            )
        }

    def handle(
        self, config: DetectorConfig, trace_id: str, revision_ms: int
    ) -> tuple[DetectorRun, ...]:
        client = self.ch()
        try:
            result = handle(
                trace_id,
                revision_ms,
                ASSEMBLER_VERSION,
                config=config,
                source=ClickHouseTraceSource(client),
                sink=ClickHouseSink(client),
            )
        finally:
            client.close()
        assert result.outcome is Outcome.OK
        return result.runs

    def view(self, trace_id: str) -> TraceView:
        client = self.ch()
        try:
            view = ClickHouseTraceSource(client).load(trace_id, ASSEMBLER_VERSION, DECLARABLE)
        finally:
            client.close()
        assert view is not None
        return view


@pytest.fixture
def pipeline(
    clickhouse: ClickHouseServer,
    database: str,
    pg_database: str,
    nats: NatsServer,
    otelcol_bin: Path,
    assembler_bin: Path,
    tmp_path: Path,
) -> Iterator[Pipeline]:
    client = clickhouse.client(database)
    migrate(client, clickhouse_migrations())
    client.close()
    with connect_postgres(pg_database) as conn:
        pg_migrate(conn, packaged(POSTGRES))
    otlp_port = _free_port()
    config = tmp_path / "collector.yaml"
    config.write_text(
        COLLECTOR_CONFIG.format(
            otlp_port=otlp_port,
            host=clickhouse.host,
            native_port=clickhouse.native_port,
            database=database,
            user=clickhouse.user,
            password=clickhouse.password,
        ),
        encoding="utf-8",
    )
    p = Pipeline(clickhouse, database, otlp_port, tmp_path)
    p.procs.append(subprocess.Popen([str(otelcol_bin), "--config", f"file:{config}"]))  # noqa: S603
    suffix = uuid.uuid4().hex[:10]
    metrics = _free_port()
    env = {
        "PATH": "/usr/bin:/bin",
        "CLICKHOUSE_ADDR": f"{clickhouse.host}:{clickhouse.native_port}",
        "CLICKHOUSE_DATABASE": database,
        "CLICKHOUSE_USER": clickhouse.user,
        "CLICKHOUSE_PASSWORD": clickhouse.password,
        "POSTGRES_DSN": pg_database,
        "NATS_URL": nats.url,
        "ASSEMBLER_STREAM": f"TC_{suffix}",
        "ASSEMBLER_SUBJECT": f"trace.completed.{suffix}",
        "ASSEMBLER_OWNER": "detectors-it",
        "ASSEMBLER_PARTITIONS": "1",
        "ASSEMBLER_GRACE": "1s",
        "ASSEMBLER_POLL_INTERVAL": "300ms",
        "ASSEMBLER_LOOKBACK": "8760h",
        "ASSEMBLER_INITIAL_LOOKBACK": "10m",
        "ASSEMBLER_METRICS_ADDR": f"127.0.0.1:{metrics}",
    }
    log = (tmp_path / "assembler.log").open("wb")
    p.procs.append(
        subprocess.Popen([str(assembler_bin)], env=env, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603
    )
    try:
        _wait(
            "collector listening",
            lambda: socket.socket().connect_ex(("127.0.0.1", otlp_port)) == 0,
            30,
        )
        yield p
    finally:
        for proc in reversed(p.procs):
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
        log.close()


def _ingest(p: Pipeline) -> dict[str, int]:
    for fx in FIXTURES:
        p.post(fx.path.read_bytes())
    rooted = {
        str(t)
        for (t,) in p.rows(
            "SELECT toString(trace_id) FROM spans GROUP BY trace_id "
            "HAVING countIf(parent_span_id = '') > 0"
        )
    }
    _wait(
        "every fixture trace assembled", lambda: len(rooted) > 10 and set(p.summaries()) >= rooted
    )
    return {t: r for t, r in p.summaries().items() if t in rooted}


def _values(name: str, value: Any) -> list[Any]:
    """A span field's string candidates: attr values, list items, or the value itself."""
    if name == "attrs":
        return list(cast("dict[str, Any]", value).values())
    return list(cast("list[Any]", value)) if isinstance(value, list) else [value]


def _value_hits(view: TraceView) -> tuple[int, dict[str, list[str]]]:
    """(string values scanned, field -> label-like values that are not known non-labels).
    Attr keys are names, not values; they are checked against the denylist elsewhere."""
    scanned = 0
    hits: dict[str, list[str]] = {}

    def scan(name: str, values: list[Any]) -> None:
        nonlocal scanned
        for v in values:
            if not isinstance(v, str):
                continue
            scanned += 1
            allowed = NOT_LABELS.get(name)
            if LABEL_LIKE.search(v) and not (allowed and allowed.fullmatch(v)):
                hits.setdefault(name, []).append(v)

    trace = view.canonical()
    for name, value in trace.items():
        if name != "spans":
            scan(name, [value])
    for span in trace["spans"]:
        for name, value in span.items():
            scan(name, _values(name, value))
    return scanned, hits


def test_the_golden_fixtures_online_and_offline_give_identical_findings(pipeline: Pipeline) -> None:
    traces = _ingest(pipeline)
    config = _config()

    # The eval marker reached ClickHouse (eval tenant), but no view carries it.
    marked = pipeline.rows(
        "SELECT count() FROM spans WHERE resource_attrs['aegis.eval.run.id'] != ''"
    )
    assert marked[0][0] > 0, (
        "the fixtures' eval run id should be stored for this test to mean anything"
    )
    scanned = values = 0
    for trace_id in traces:
        n, hits = _value_hits(pipeline.view(trace_id))
        assert hits == {}, (trace_id, hits)
        scanned, values = scanned + 1, values + n
    assert scanned == len(traces) > 10
    assert values > 1000
    # The scan can fail: a view that kept the marker would be caught.
    leak = pipeline.view(sorted(traces)[0])
    sabotaged = replace(leak, bundle_digest="faults-2026-11-05:tool_failure:0123456789ab")
    assert _value_hits(sabotaged)[1] == {"bundle_digest": [sabotaged.bundle_digest]}

    online: dict[tuple[str, str], DetectorRun] = {}
    for trace_id, revision in sorted(traces.items()):
        for run in pipeline.handle(config, trace_id, revision):
            online[(run.trace_id, run.detector)] = run
    assert len(online) == 2 * len(traces)

    snap = pipeline.tmp_path / "snap"
    ids = pipeline.tmp_path / "ids.txt"
    ids.write_text("\n".join(sorted(traces)) + "\n", encoding="utf-8")
    assert (
        detectors_cli(
            [
                "snapshot-export",
                "--clickhouse-url",
                pipeline.url,
                "--assembler-version",
                ASSEMBLER_VERSION,
                "--out",
                str(snap),
                "--trace-ids-file",
                str(ids),
                "--repo",
                str(ROOT),
            ]
        )
        == 0
    )
    manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["trace_count"] == len(traces)
    assert [m["version"] for m in manifest["clickhouse_migrations"]] == [
        m.version for m in clickhouse_migrations()
    ]
    cfg = pipeline.tmp_path / "detectors.yaml"
    cfg.write_text("detectors:\n  reference_root: {}\n  reference_baseline: {}\n", encoding="utf-8")
    out = pipeline.tmp_path / "offline.jsonl"
    argv = ["run-offline", "--snapshot", str(snap), "--config", str(cfg), "--out", str(out)]
    assert detectors_cli(argv, _registry()) == 0

    compared = findings = 0
    for line in out.read_text(encoding="utf-8").splitlines():
        run = json.loads(line)
        mine = online[(run["trace_id"], run["detector"])]
        assert (run["input_digest"], run["findings_digest"], run["detector_version"]) == (
            mine.input_digest,
            mine.findings_digest,
            mine.detector_version,
        ), run["trace_id"]
        compared += 1
        findings += len(run["findings"])
    assert compared == len(online)
    assert findings > 0

    # The stored runs are the ones computed online, and every version resolves to its params.
    stored = pipeline.rows(
        "SELECT toString(trace_id), detector, input_digest, findings_digest "
        "FROM detector_runs FINAL"
    )
    assert {(t, d): (i, f) for t, d, i, f in stored} == {
        k: (r.input_digest, r.findings_digest) for k, r in online.items()
    }
    client = pipeline.ch()
    try:
        ClickHouseSink(client).register(config.detectors)  # same params: no drift
    finally:
        client.close()
    unresolved = pipeline.rows(
        "SELECT count() FROM (SELECT DISTINCT detector, detector_version FROM findings) AS f "
        "LEFT ANTI JOIN detector_params AS p USING (detector, detector_version)"
    )
    checked = pipeline.rows("SELECT count(DISTINCT detector, detector_version) FROM findings")
    assert unresolved == [(0,)]
    assert checked[0][0] == 1


class _CrashBeforeMarker:
    """A client that dies when the run rows (the commit marker) are written."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def command(self, sql: str, parameters: Any = None) -> object:
        if "INSERT INTO detector_runs" in sql:
            raise ConnectionError("crash before the commit marker")
        return self._client.command(sql, parameters)

    def query_rows(self, sql: str, parameters: Any = None) -> list[tuple[Any, ...]]:
        return self._client.query_rows(sql, parameters)


def test_a_late_span_supersedes_and_a_crash_before_the_marker_loses_nothing(
    pipeline: Pipeline,
) -> None:
    traces = _ingest(pipeline)
    config = _config()
    trace_id = sorted(traces)[0]
    first = pipeline.handle(config, trace_id, traces[trace_id])
    current = _statement("detectors_findings_current_read.sql")
    before = pipeline.rows(current, {"trace_id": trace_id})
    assert before
    assert {r[4] for r in before} == {traces[trace_id]}

    # A late child of the root: the assembler re-assembles with a newer revision.
    view = pipeline.view(trace_id)
    late = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": view.service}},
            ]},
            "scopeSpans": [{"spans": [{
                "traceId": trace_id, "spanId": "c0ffee00c0ffee00",
                "parentSpanId": view.root_span_id,
                "name": "late span", "kind": 1,
                "startTimeUnixNano": str(view.start_us * 1000 + 1000),
                "endTimeUnixNano": str(view.start_us * 1000 + 2000),
                "status": {"code": 2},
            }]}],
        }]
    }  # fmt: skip
    pipeline.post(json.dumps(late).encode())
    _wait("re-assembly", lambda: pipeline.summaries()[trace_id] > traces[trace_id])
    revision = pipeline.summaries()[trace_id]

    # A crash after the findings and before the run rows: the old revision stays current.
    client = pipeline.ch()
    try:
        with pytest.raises(ConnectionError):
            handle(
                trace_id,
                revision,
                ASSEMBLER_VERSION,
                config=config,
                source=ClickHouseTraceSource(client),
                sink=ClickHouseSink(_CrashBeforeMarker(client)),
            )
    finally:
        client.close()
    assert pipeline.rows(current, {"trace_id": trace_id}) == before

    # The redelivery runs again and commits; the late span (status Error) is a new finding.
    second = pipeline.handle(config, trace_id, revision)
    after = pipeline.rows(current, {"trace_id": trace_id})
    assert {r[4] for r in after} == {revision}
    assert {r[1] for r in after} == {f.span_id for r in second for f in r.findings}
    assert "c0ffee00c0ffee00" in {r[1] for r in after}
    assert first[0].input_digest != second[0].input_digest

    # Another redelivery of the same revision writes the same rows: nothing new after FINAL.
    count = pipeline.rows("SELECT count() FROM findings FINAL")
    pipeline.handle(config, trace_id, revision)
    assert pipeline.rows("SELECT count() FROM findings FINAL") == count


def test_the_old_findings_read_returns_one_row_per_revision(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """Code N-1's read (contract/findings_read.sql) on schema N: revision is in the sorting key,
    so FINAL keeps one row per revision of a span. The old read has no revision column and is
    superseded by detectors_findings_current_read.sql (ADR-0010); nothing on main reads findings
    besides this contract harness."""
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        old_read = next(s for n, s in contract.packaged() if n == "findings_read.sql")
        write = _statement("detectors_findings_write.sql")
        params = contract.parameters(write)
        for revision, details in (
            (params["revision_ms"], '{"r":1}'),
            (params["revision_ms"] + 1, '{"r":2}'),
        ):
            client.command(write, {**params, "revision_ms": revision, "details": [details],
                                   "finding_ids": [str(uuid.uuid4())]})  # fmt: skip
        rows = client.query_rows(old_read)
        print("\nold findings_read.sql on schema N:", *rows, sep="\n  ")
        assert len(rows) == 2
        # FixedString columns come back as bytes from clickhouse-connect.
        assert {(bytes(r[1]).decode(), r[2], r[3]) for r in rows} == {
            ("b7ad6b7169203331", "context_truncation", "0.1.0+p0123abcd")
        }
        assert sorted(r[7] for r in rows) == ['{"r":1}', '{"r":2}']
        current = client.query_rows(_statement("detectors_findings_current_read.sql"),
                                    {"trace_id": params["trace_id"]})  # fmt: skip
        assert current == [], "no run marker yet: the current read shows nothing"
    finally:
        client.close()


# --- the identity of a committed run (Codex review on #69) ---------------------------------------

OTHER_BASELINE = ServiceBaseline(
    service="agentic-rag",
    bundle_digest="sha256:" + "b" * 64,
    window_start_us=1_793_000_000_000_000,
    window_end_us=1_793_600_000_000_000,
    trace_count=120,
)


def _current(client: Any) -> list[tuple[Any, ...]]:
    return client.query_rows(
        _statement("detectors_findings_current_read.sql"), {"trace_id": TRACE_ID}
    )


def _write(client: Any, runs: Sequence[DetectorRun]) -> None:
    ClickHouseSink(client).write(runs)


def test_a_rerun_with_other_context_and_no_findings_leaves_no_current_finding(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """(a) Same revision and input; the baseline changes between the runs, and the second run
    finds nothing: its marker must not select the first run's finding."""
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        config = load_config({"detectors": {"reference_baseline": {}}}, _registry())
        view = build_view(summary_row(), span_rows(), DECLARABLE)
        first = run_trace(
            view, config.detectors, Providers(StaticBaselineProvider([OTHER_BASELINE]))
        )
        second = run_trace(view, config.detectors, Providers())
        assert len(first[0].findings) == 1
        assert second[0].findings == ()
        assert first[0].input_digest == second[0].input_digest
        _write(client, first)
        assert len(_current(client)) == 1
        _write(client, second)
        assert _current(client) == []
    finally:
        client.close()


def test_a_failed_retry_selects_no_findings(clickhouse: ClickHouseServer, database: str) -> None:
    """(b) An ok run, then a retry with the same inputs that fails: fail closed."""
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        config = load_config({"detectors": {"reference_root": {}}}, _registry())
        view = build_view(summary_row(), span_rows(), DECLARABLE)
        (ok,) = run_trace(view, config.detectors, Providers())
        assert ok.findings
        _write(client, [ok])
        assert len(_current(client)) == len(ok.findings)
        failed = replace(ok, status=RunStatus.ERROR, error_kind="RuntimeError", findings=())
        _write(client, [failed])
        assert _current(client) == []
    finally:
        client.close()


def test_uncommitted_findings_of_another_projection_never_replace_committed_ones(
    clickhouse: ClickHouseServer, database: str
) -> None:
    """(c) Two projections of one revision (a late span visible before the summary moved on).
    The second run writes its findings and crashes before its marker: after the merges, the
    first run's committed findings are still the current ones."""
    client = clickhouse.client(database)
    try:
        migrate(client, clickhouse_migrations())
        config = load_config({"detectors": {"reference_root": {}}}, _registry())
        rows = span_rows()
        late = {**rows[1], "span_id": "4444444444444444", "start_us": rows[1]["start_us"] + 5}
        first = run_trace(
            build_view(summary_row(), rows, DECLARABLE), config.detectors, Providers()
        )
        second = run_trace(
            build_view(summary_row(), [*rows, late], DECLARABLE), config.detectors, Providers()
        )
        assert first[0].revision_ms == second[0].revision_ms
        assert first[0].input_digest != second[0].input_digest
        assert {f.span_id for f in first[0].findings} & {f.span_id for f in second[0].findings}
        _write(client, first)
        committed = sorted(r[0] for r in _current(client))
        with pytest.raises(ConnectionError):
            ClickHouseSink(_CrashBeforeMarker(client)).write(second)
        client.command("OPTIMIZE TABLE findings FINAL")
        assert sorted(r[0] for r in _current(client)) == committed
        assert committed
    finally:
        client.close()
