"""The trace-assembler end to end (ADR-0003, #35): the golden fixtures go through the built
collector into ClickHouse, the built assembler writes trace_summary and publishes
trace.completed to JetStream, the watermark lives in Postgres. All three are test containers."""

import asyncio
import copy
import json
import os
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import nats as nats_client
import psycopg
import pytest
from jsonschema import Draft202012Validator, ValidationError
from testcontainers.core.container import DockerContainer

from traceblame_contracts.fixtures import discover_fixtures
from traceblame_storage.client import connect_postgres
from traceblame_storage.migrations import POSTGRES, packaged
from traceblame_storage.migrations import packaged as clickhouse_migrations
from traceblame_storage.pg_runner import migrate as pg_migrate
from traceblame_storage.runner import migrate as ch_migrate
from traceblame_storage.testing import ClickHouseServer, NatsServer

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = discover_fixtures(ROOT / "collector" / "testdata" / "fixtures")
SCHEMA = json.loads(
    (ROOT / "schemas" / "events" / "trace.completed.v1.schema.json").read_text(encoding="utf-8")
)
VERSION = "1.0.0"
LEASE_TTL_S = 3

COLLECTOR_CONFIG = """
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{otlp_port}
processors:
  normalizer:
    tenant_id: integration
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
      encoding: json
      output_paths: [{log}]
      error_output_paths: [{log}]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [normalizer]
      exporters: [clickhouse]
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _wait(what: str, check: Callable[[], bool], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not happen within {timeout:.0f} s")
        time.sleep(0.25)


@dataclass
class Assembler:
    proc: subprocess.Popen[bytes]
    owner: str
    metrics_port: int
    log: Path

    def metrics(self) -> str:
        url = f"http://127.0.0.1:{self.metrics_port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return str(resp.read().decode("utf-8"))

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=30)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=30)


@dataclass
class Stack:
    clickhouse: ClickHouseServer
    database: str
    pg_url: str
    nats: NatsServer
    stream: str
    subject: str
    otlp_port: int
    assembler_bin: Path
    tmp_path: Path
    assemblers: list[Assembler] = field(default_factory=list[Assembler])

    def ch(self) -> Any:
        return self.clickhouse.client(self.database)

    def post(self, doc: dict[str, Any] | bytes) -> None:
        body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.otlp_port}/v1/traces",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            assert resp.status == 200

    def ingest_fixtures(self) -> None:
        for fx in FIXTURES:
            self.post(fx.path.read_bytes())
        self.wait_for_spans(sum(self._fixture_span_counts()))

    def _fixture_span_counts(self) -> list[int]:
        return [
            len(ss["spans"])
            for fx in FIXTURES
            for rs in fx.load()["resourceSpans"]
            for ss in rs["scopeSpans"]
        ]

    def wait_for_spans(self, expected: int) -> None:
        client = self.ch()
        try:
            _wait(
                f"{expected} spans in ClickHouse",
                lambda: client.query_rows("SELECT count() FROM spans")[0][0] >= expected,
            )
        finally:
            client.close()

    def start_assembler(self, owner: str, partitions: int = 2, **env: str) -> Assembler:
        port = _free_port()
        log = self.tmp_path / f"assembler-{owner}.log"
        full = {
            **os.environ,
            "CLICKHOUSE_ADDR": f"{self.clickhouse.host}:{self.clickhouse.native_port}",
            "CLICKHOUSE_DATABASE": self.database,
            "CLICKHOUSE_USER": self.clickhouse.user,
            "CLICKHOUSE_PASSWORD": self.clickhouse.password,
            "POSTGRES_DSN": self.pg_url,
            "NATS_URL": self.nats.url,
            "ASSEMBLER_STREAM": self.stream,
            "ASSEMBLER_SUBJECT": self.subject,
            "ASSEMBLER_OWNER": owner,
            "ASSEMBLER_PARTITIONS": str(partitions),
            "ASSEMBLER_GRACE": "1s",
            "ASSEMBLER_POLL_INTERVAL": "300ms",
            "ASSEMBLER_LEASE_TTL": f"{LEASE_TTL_S}s",
            "ASSEMBLER_RECONCILE_EVERY": "1s",
            "ASSEMBLER_METRICS_ADDR": f"127.0.0.1:{port}",
            **env,
        }
        with log.open("wb") as out:
            proc = subprocess.Popen(  # noqa: S603 - the binary under test
                [str(self.assembler_bin)], env=full, stdout=out, stderr=subprocess.STDOUT
            )
        asm = Assembler(proc, owner, port, log)
        self.assemblers.append(asm)

        def ready() -> bool:
            if proc.poll() is not None:
                pytest.fail(f"assembler {owner} exited:\n{log.read_text('utf-8')}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=1) as resp:
                    return bool(resp.status == 200)
            except OSError:
                return False

        _wait(f"assembler {owner} ready", ready, 30)
        return asm

    # --- what the run produced -------------------------------------------------------------

    def expected(self) -> dict[str, int]:
        """trace id -> span count, for the traces in `spans` that have a root span."""
        client = self.ch()
        try:
            rows = client.query_rows(
                "SELECT toString(trace_id), count(), countIf(parent_span_id = '') "
                "FROM spans GROUP BY trace_id"
            )
        finally:
            client.close()
        return {t: int(n) for t, n, roots in rows if roots > 0}

    def summaries(self) -> dict[str, tuple[int, int]]:
        """trace id -> (revision ms, span count) of the newest summary."""
        client = self.ch()
        try:
            rows = client.query_rows(
                "SELECT toString(trace_id), toUnixTimestamp64Milli(max(revision)), "
                "argMax(span_count, revision) FROM trace_summary GROUP BY trace_id"
            )
        finally:
            client.close()
        return {t: (int(r), int(n)) for t, r, n in rows}

    def wait_assembled(self, expected: dict[str, int], timeout: float = 90) -> None:
        _wait(
            f"{len(expected)} traces assembled",
            lambda: {t: n for t, (_, n) in self.summaries().items()} == expected,
            timeout,
        )

    def events(self) -> list[dict[str, Any]]:
        return asyncio.run(_read_events(self.nats.url, self.stream))

    def wait_events(self, count: int, timeout: float = 60) -> list[dict[str, Any]]:
        got: list[dict[str, Any]] = []

        def enough() -> bool:
            got[:] = self.events()
            return len(got) >= count

        _wait(f"{count} events on {self.stream}", enough, timeout)
        return got

    def watermarks(self) -> dict[int, tuple[int, str | None, int]]:
        with psycopg.connect(self.pg_url, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT partition, watermark_ms, owner, epoch FROM assembler_partitions"
            ).fetchall()
        return {int(p): (int(w), o, int(e)) for p, w, o, e in rows}


async def _read_events(url: str, stream: str) -> list[dict[str, Any]]:
    # nats-py leaves its option and header types open; the client is used as Any here, once.
    connect = cast("Callable[[str], Awaitable[Any]]", nats_client.connect)  # pyright: ignore[reportUnknownMemberType]  # nats-py leaves connect's options untyped
    nc: Any = await connect(url)
    try:
        js: Any = nc.jetstream()
        try:
            info = await js.stream_info(stream)
        except Exception:  # no stream yet means no events yet
            return []
        out: list[dict[str, Any]] = []
        if info.state.messages:
            for seq in range(info.state.first_seq, info.state.last_seq + 1):
                msg: Any = await js.get_msg(stream, seq=seq)
                headers: dict[str, str] = dict(msg.headers or {})
                out.append(
                    {
                        "payload": json.loads(msg.data or b"{}"),
                        "msg_id": headers.get("Nats-Msg-Id"),
                        "seq": seq,
                    }
                )
        return out
    finally:
        await nc.close()


@pytest.fixture
def stack(
    clickhouse: ClickHouseServer,
    database: str,
    pg_database: str,
    nats: NatsServer,
    otelcol_bin: Path,
    assembler_bin: Path,
    tmp_path: Path,
) -> Iterator[Stack]:
    client = clickhouse.client(database)
    ch_migrate(client, clickhouse_migrations())
    client.close()
    with connect_postgres(pg_database) as conn:
        pg_migrate(conn, packaged(POSTGRES))

    suffix = uuid.uuid4().hex[:10]
    otlp_port, collector_log = _free_port(), tmp_path / "collector.log"
    config = tmp_path / "collector.yaml"
    config.write_text(
        COLLECTOR_CONFIG.format(
            otlp_port=otlp_port,
            host=clickhouse.host,
            native_port=clickhouse.native_port,
            database=database,
            user=clickhouse.user,
            password=clickhouse.password,
            log=collector_log,
        ),
        encoding="utf-8",
    )
    collector = subprocess.Popen([str(otelcol_bin), "--config", f"file:{config}"])  # noqa: S603
    s = Stack(
        clickhouse=clickhouse,
        database=database,
        pg_url=pg_database,
        nats=nats,
        stream=f"TC_{suffix}",
        subject=f"trace.completed.{suffix}",
        otlp_port=otlp_port,
        assembler_bin=assembler_bin,
        tmp_path=tmp_path,
    )
    try:
        _wait(
            "collector listening",
            lambda: socket.socket().connect_ex(("127.0.0.1", otlp_port)) == 0,
            30,
        )
        yield s
    finally:
        for asm in s.assemblers:
            asm.stop()
        collector.send_signal(signal.SIGTERM)
        collector.wait(timeout=30)


def _late_child(trace_doc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A document with one more span for the trace: a child of its root, exported later."""
    doc = copy.deepcopy(trace_doc)
    spans = doc["resourceSpans"][0]["scopeSpans"][0]["spans"]
    root = next(sp for sp in spans if not sp.get("parentSpanId"))
    late = copy.deepcopy(root)
    late["spanId"] = "c0ffee00c0ffee00"
    late["parentSpanId"] = root["spanId"]
    late["name"] = "late span"
    doc["resourceSpans"][0]["scopeSpans"][0]["spans"] = [late]
    return str(root["traceId"]), doc


def _validate(payload: dict[str, Any]) -> None:
    errors = list(Draft202012Validator(SCHEMA).iter_errors(payload))  # pyright: ignore[reportUnknownMemberType]  # jsonschema's stubs leave iter_errors partially unknown
    if errors:
        raise ValidationError(errors[0].message)


# --- tests ---------------------------------------------------------------------------------


def test_every_golden_fixture_trace_is_assembled_with_one_event_per_revision(stack: Stack) -> None:
    stack.ingest_fixtures()
    expected = stack.expected()
    assert len(expected) >= 10, "the fixtures hold many traces"

    stack.start_assembler("a")
    stack.wait_assembled(expected)
    events = stack.wait_events(len(expected))

    for e in events:
        _validate(e["payload"])
        p = e["payload"]
        assert e["msg_id"] == f"{p['trace_id']}/{p['assembler_version']}/{p['revision']}"
        assert p["assembler_version"] == VERSION
    per_revision = Counter((e["payload"]["trace_id"], e["payload"]["revision"]) for e in events)
    assert max(per_revision.values()) == 1, "one trace.completed per revision"
    assert {e["payload"]["trace_id"] for e in events} == set(expected)
    assert len(events) == len(expected), "no trace has two revisions without late spans"
    assert {e["payload"]["trace_id"]: e["payload"]["span_count"] for e in events} == expected

    # the revision is the trace's newest arrival, the summary carries the root's facts
    client = stack.ch()
    try:
        arrivals = dict(
            client.query_rows(
                "SELECT toString(trace_id), toUnixTimestamp64Milli(max(ingested_at)) "
                "FROM trace_arrivals GROUP BY trace_id"
            )
        )
        by_event = {e["payload"]["trace_id"]: e["payload"]["revision"] for e in events}
        assert all(by_event[t] == arrivals[t] for t in expected)
        rows = client.query_rows(
            "SELECT toString(trace_id), service, tenant_id, toString(root_span_id), span_count, "
            "error_span_count FROM trace_summary FINAL"
        )
        errors = dict(
            client.query_rows(
                "SELECT toString(trace_id), countIf(lower(status_code) = 'error') FROM spans "
                "GROUP BY trace_id"
            )
        )
        roots = dict(
            client.query_rows(
                "SELECT toString(trace_id), toString(argMin(span_id, start_ts)) FROM spans "
                "WHERE parent_span_id = '' GROUP BY trace_id"
            )
        )
        for trace, service, tenant, root_span, spans, error_spans in rows:
            assert (tenant, spans) == ("integration", expected[trace]), trace
            assert service != "", trace
            assert root_span == roots[trace]
            assert error_spans == errors[trace]
    finally:
        client.close()

    # the watermark moved past every revision and the metrics saw the work
    assert all(w > max(by_event.values()) for w, _, _ in stack.watermarks().values())
    text = stack.assemblers[0].metrics()
    assert 'traceblame_assembler_assembled_traces_total{kind="first"}' in text
    assert "traceblame_assembler_watermark_lag_seconds" in text


def test_a_late_span_re_assembles_the_trace_with_a_higher_revision(stack: Stack) -> None:
    stack.ingest_fixtures()
    expected = stack.expected()
    stack.start_assembler("a")
    stack.wait_assembled(expected)
    before = stack.wait_events(len(expected))

    fx = next(f for f in FIXTURES if f.id.endswith("/chat_basic"))
    trace, doc = _late_child(fx.load())
    old_revision, old_count = stack.summaries()[trace]
    stack.post(doc)

    _wait(
        "the late span re-assembled",
        lambda: stack.summaries()[trace][1] == old_count + 1,
    )
    new_revision, _ = stack.summaries()[trace]
    assert new_revision > old_revision

    events = stack.wait_events(len(before) + 1)
    mine = [e["payload"] for e in events if e["payload"]["trace_id"] == trace]
    assert [p["revision"] for p in mine] == [old_revision, new_revision]
    assert [p["span_count"] for p in mine] == [old_count, old_count + 1]
    others = Counter(e["payload"]["trace_id"] for e in events if e["payload"]["trace_id"] != trace)
    assert set(others.values()) == {1}, "no other trace was announced again"
    text = stack.assemblers[0].metrics()
    assert 'traceblame_assembler_assembled_traces_total{kind="again"}' in text


def test_a_crash_between_the_summary_write_and_the_watermark_loses_and_doubles_nothing(
    stack: Stack, nats_container: DockerContainer
) -> None:
    """Pause JetStream so the publish stalls after the summary is written, kill the assembler
    there, then let a new one resume: every trace is announced exactly once."""
    first = stack.start_assembler("a")
    _wait("a window committed", lambda: all(w > 0 for w, _, _ in stack.watermarks().values()))

    container = nats_container.get_wrapped_container()
    assert container is not None
    container.pause()
    try:
        stack.ingest_fixtures()
        expected = stack.expected()
        _wait("summaries written while publishing stalls", lambda: bool(stack.summaries()))
        first.kill()
        stuck = stack.summaries()
        # the commit point was not reached for any trace that was summarised
        client = stack.ch()
        try:
            partition_of = dict(
                client.query_rows(
                    "SELECT toString(trace_id), cityHash64(trace_id) % 2 FROM trace_summary"
                )
            )
        finally:
            client.close()
        marks = stack.watermarks()
        for trace, (revision, _) in stuck.items():
            assert marks[int(partition_of[trace])][0] < revision, trace
    finally:
        container.unpause()

    time.sleep(1)
    stack.start_assembler("b")  # takes the partitions over once a's lease expires
    stack.wait_assembled(expected, timeout=120)
    events = stack.wait_events(len(expected), timeout=120)

    per_trace = Counter(e["payload"]["trace_id"] for e in events)
    assert set(per_trace) == set(expected), "no event lost"
    assert set(per_trace.values()) == {1}, "no event doubled"
    _wait(
        "the watermark passed every revision",
        lambda: all(
            stack.watermarks()[int(partition_of[t])][0] >= r
            for t, (r, _) in stack.summaries().items()
        ),
    )


def test_replicas_share_the_partitions_and_the_survivor_takes_over(stack: Stack) -> None:
    a = stack.start_assembler("a", partitions=4)
    b = stack.start_assembler("b", partitions=4)
    stack.ingest_fixtures()
    expected = stack.expected()
    stack.wait_assembled(expected)
    stack.wait_events(len(expected))

    owners = Counter(o for _, o, _ in stack.watermarks().values())
    assert set(owners) <= {"a", "b"}
    victim, survivor = (a, b) if owners["a"] >= owners["b"] else (b, a)
    victim.kill()

    # new traces after the failure: a copy of a fixture with fresh trace ids
    fx = next(f for f in FIXTURES if f.id.endswith("/chat_basic"))
    fresh: dict[str, int] = {}
    for i in range(12):
        doc = copy.deepcopy(fx.load())
        trace = f"{0xFEED0000 + i:032x}"
        for rs in doc["resourceSpans"]:
            for ss in rs["scopeSpans"]:
                for span in ss["spans"]:
                    span["traceId"] = trace
        fresh[trace] = sum(
            len(ss["spans"]) for rs in doc["resourceSpans"] for ss in rs["scopeSpans"]
        )
        stack.post(doc)
    expected = {**expected, **fresh}
    stack.wait_for_spans(sum(expected.values()))
    stack.wait_assembled(expected, timeout=120)
    events = stack.wait_events(len(expected), timeout=120)
    assert set(Counter(e["payload"]["trace_id"] for e in events).values()) == {1}
    assert survivor.proc.poll() is None
    final_owners = {o for _, o, _ in stack.watermarks().values()}
    assert final_owners == {survivor.owner}, "the survivor owns every partition"


def test_a_bad_configuration_stops_the_assembler_at_startup(stack: Stack) -> None:
    proc = subprocess.run(  # noqa: S603 - the binary under test
        [str(stack.assembler_bin)],
        env={**os.environ, "ASSEMBLER_PARTITIONS": "0"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 1
    assert "ASSEMBLER_PARTITIONS must be >= 1" in proc.stderr
    assert "POSTGRES_DSN is required" in proc.stderr
