"""No prompt or completion text reaches any ClickHouse table (issue #11, ADR-0002).

Every golden fixture, content-on ones included, goes through the built collector
(OTLP/HTTP -> normalizer -> stock clickhouse exporter) into a migrated database. Then every
row of every table, in every column, is searched for the capture's text.
"""

import copy
import json
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from traceblame_contracts.fixtures import discover_fixtures
from traceblame_contracts.otlp import flatten_otlp_json
from traceblame_storage.migrations import packaged
from traceblame_storage.runner import migrate
from traceblame_storage.testing import ClickHouseServer

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = discover_fixtures(ROOT / "collector" / "testdata" / "fixtures")
# The capture's prompts and answers (tools/fixtures/capture.py), and what the captured errors
# carry: tracebacks with local paths and the server's message that echoes the request.
NEEDLES = ("France", "Paris", "weather", "Traceback", "/home/", "too short")
# trace_roots goes in the contract phase after #39.
TABLES = (
    "spans",
    "trace_roots",
    "trace_arrivals",
    "findings",
    "blame",
    "injections",
    "schema_migrations",
)
# Tenant variants of one fixture, with fresh trace ids: the shim sets aegis.tenant.id on the
# resource (config mode keeps it), or on span attributes (the wrong scope: dropped, and config
# mode fills the resource).
SHIM_TRACE = "7e5a0000000000000000000000000001"
SPAN_ATTR_TRACE = "7e5a0000000000000000000000000002"

CONFIG = """
receivers:
  otlp:
    protocols:
      http:
        endpoint: 127.0.0.1:{otlp_port}
processors:
  normalizer:
    tenant_id: integration
exporters:
  file:
    path: {normalized}
    format: json
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
      level: debug
      encoding: json
      output_paths: [{log}]
      error_output_paths: [{log}]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [normalizer]
      exporters: [clickhouse, file]
"""


def _tenant_variants() -> list[dict[str, Any]]:
    base = next(
        f for f in FIXTURES if f.id.startswith("otel-genai/") and f.id.endswith("/chat_basic")
    )
    out: list[dict[str, Any]] = []
    for trace_id, on_resource, tenant in (
        (SHIM_TRACE, True, "tenant-shim"),
        (SPAN_ATTR_TRACE, False, "tenant-span"),
    ):
        doc = copy.deepcopy(base.load())
        attr = {"key": "aegis.tenant.id", "value": {"stringValue": tenant}}
        for rs in doc["resourceSpans"]:
            if on_resource:
                rs["resource"].setdefault("attributes", []).append(attr)
            for ss in rs["scopeSpans"]:
                for span in ss["spans"]:
                    span["traceId"] = trace_id
                    if not on_resource:
                        span.setdefault("attributes", []).append(attr)
        out.append(doc)
    return out


def _posted() -> list[bytes]:
    """What the run sends: every fixture as captured, then the tenant variants."""
    return [fx.path.read_bytes() for fx in FIXTURES] + [
        json.dumps(doc).encode() for doc in _tenant_variants()
    ]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def loaded(
    clickhouse: ClickHouseServer, database: str, otelcol_bin: Path, tmp_path: Path
) -> tuple[ClickHouseServer, str, str]:
    """The database after every fixture went through the collector; and the collector log."""
    client = clickhouse.client(database)
    migrate(client, packaged())
    otlp_port, log = _free_port(), tmp_path / "collector.log"
    config = tmp_path / "config.yaml"
    config.write_text(
        CONFIG.format(
            otlp_port=otlp_port,
            host=clickhouse.host,
            native_port=clickhouse.native_port,
            database=database,
            user=clickhouse.user,
            password=clickhouse.password,
            log=log,
            normalized=tmp_path / "normalized.jsonl",
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen([str(otelcol_bin), "--config", f"file:{config}"])  # noqa: S603
    try:
        deadline = time.monotonic() + 30
        while socket.socket().connect_ex(("127.0.0.1", otlp_port)) != 0:
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail(
                    f"collector did not start:\n{log.read_text('utf-8') if log.exists() else ''}"
                )
            time.sleep(0.2)
        expected = 0
        for body in _posted():
            expected += len(flatten_otlp_json(json.loads(body)))
            req = urllib.request.Request(
                f"http://127.0.0.1:{otlp_port}/v1/traces",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                assert resp.status == 200
        deadline = time.monotonic() + 60
        while client.query_rows("SELECT count() FROM spans")[0][0] < expected:
            if time.monotonic() > deadline:
                pytest.fail("not every span reached ClickHouse within 60 s")
            time.sleep(0.5)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    assert client.query_rows("SELECT count() FROM spans")[0][0] == expected
    client.close()
    return clickhouse, database, log.read_text("utf-8")


def _text(v: Any) -> Any:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else v  # FixedString


def _rows(clickhouse: ClickHouseServer, database: str, table: str) -> list[dict[str, Any]]:
    client = clickhouse.client(database)
    try:
        names = [
            r[0]
            for r in client.query_rows(
                "SELECT name FROM system.columns "  # noqa: S608 - test names, not user input
                f"WHERE database = '{database}' AND table = '{table}' ORDER BY position"
            )
        ]
        rows = client.query_rows(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
        return [dict(zip(names, map(_text, r), strict=True)) for r in rows]
    finally:
        client.close()


def test_the_content_fixtures_carry_content() -> None:
    """Otherwise the test below proves nothing."""
    content = [fx for fx in FIXTURES if fx.provenance().content_capture]
    assert content
    for fx in content:
        text = fx.path.read_text(encoding="utf-8")
        assert any(n in text for n in NEEDLES), fx.id


def test_no_row_in_any_table_holds_content(loaded: tuple[ClickHouseServer, str, str]) -> None:
    clickhouse, database, log = loaded
    for table in TABLES:
        text = json.dumps(_rows(clickhouse, database, table), default=str, ensure_ascii=False)
        assert [n for n in NEEDLES if n in text] == [], table
    assert [n for n in NEEDLES if n in log] == [], "collector log"


def test_the_ingest_table_stores_nothing(loaded: tuple[ClickHouseServer, str, str]) -> None:
    clickhouse, database, _ = loaded
    client = clickhouse.client(database)
    try:
        assert client.query_rows("SELECT count() FROM otel_traces_ingest") == [(0,)]
    finally:
        client.close()


def test_spans_carry_the_normalized_fields(loaded: tuple[ClickHouseServer, str, str]) -> None:
    clickhouse, database, _ = loaded
    by_trace = {r["trace_id"]: r for r in _rows(clickhouse, database, "spans")}
    error = next(
        f for f in FIXTURES if f.id.startswith("openinference/") and f.id.endswith("/chat_error")
    )
    row = by_trace[flatten_otlp_json(error.load())[0]["trace_id"]]
    assert row["span_kind"] == "llm"
    assert row["model"] == "qwen2.5:7b"
    assert row["provider"] == "openai"
    assert row["status_code"] == "Error"
    assert (row["error_type"], row["http_status_code"]) == ("BadRequestError", 400)
    assert row["tenant_id"] == "integration"
    assert row["source_sdk"] == "openinference"
    assert row["profile_version"] == "aegis-telemetry/1"

    chat = next(
        f for f in FIXTURES if f.id.startswith("otel-genai/") and f.id.endswith("/chat_basic")
    )
    row = by_trace[flatten_otlp_json(chat.load())[0]["trace_id"]]
    assert row["finish_reasons"] == ["stop"]
    assert (row["input_tokens"], row["output_tokens"]) == (38, 2)
    assert row["operation"] == "chat"
    assert row["http_status_code"] == 0


def test_trace_roots_hold_one_row_per_root_span(loaded: tuple[ClickHouseServer, str, str]) -> None:
    """Until the contract phase after #39 drops trace_roots, it keeps being fed."""
    clickhouse, database, _ = loaded
    roots = {
        s["trace_id"]
        for body in _posted()
        for s in flatten_otlp_json(json.loads(body))
        if not s["parent_span_id"]
    }
    rows = _rows(clickhouse, database, "trace_roots")
    assert {r["trace_id"] for r in rows} == roots
    assert all(r["end_ts"] >= r["start_ts"] for r in rows)


def test_trace_arrivals_mark_every_trace_and_its_root(
    loaded: tuple[ClickHouseServer, str, str],
) -> None:
    clickhouse, database, _ = loaded
    spans = [s for body in _posted() for s in flatten_otlp_json(json.loads(body))]
    rows = _rows(clickhouse, database, "trace_arrivals")
    assert {r["trace_id"] for r in rows} == {s["trace_id"] for s in spans}
    assert {r["trace_id"] for r in rows if r["is_root"]} == {
        s["trace_id"] for s in spans if not s["parent_span_id"]
    }
    assert all(
        set(r) == {"trace_id", "ingested_at", "is_root", "service", "tenant_id"} for r in rows
    ), "minimal columns, nothing else of a span"


def _normalized_tenants(path: Path) -> dict[tuple[str, str], set[str]]:
    """aegis.tenant.id per (trace, service) in the normalizer's output (the file exporter)."""
    out: dict[tuple[str, str], set[str]] = {}
    for line in path.read_text("utf-8").splitlines():
        for rs in json.loads(line)["resourceSpans"]:
            res = {a["key"]: a["value"] for a in rs["resource"].get("attributes", [])}
            service = res["service.name"]["stringValue"]
            tenant = res.get("aegis.tenant.id", {}).get("stringValue", "")
            for ss in rs["scopeSpans"]:
                for span in ss["spans"]:
                    keys = {a["key"] for a in span.get("attributes", [])}
                    assert "aegis.tenant.id" not in keys, "the tenant is a resource attribute"
                    out.setdefault((span["traceId"], service), set()).add(tenant)
    return out


def test_trace_arrivals_carry_the_normalized_tenant(
    loaded: tuple[ClickHouseServer, str, str], tmp_path: Path
) -> None:
    """Single-tenant (config mode) run: every arrival's tenant_id is non-empty and is its spans'
    normalized aegis.tenant.id, whether config mode filled it or the shim set it."""
    clickhouse, database, _ = loaded
    normalized = _normalized_tenants(tmp_path / "normalized.jsonl")
    rows = _rows(clickhouse, database, "trace_arrivals")
    assert {(r["trace_id"], r["service"]) for r in rows} == set(normalized)
    for r in rows:
        assert r["tenant_id"], r
        assert {r["tenant_id"]} == normalized[(r["trace_id"], r["service"])], r
    tenants = {r["trace_id"]: r["tenant_id"] for r in rows}
    assert tenants[SHIM_TRACE] == "tenant-shim", "config mode keeps the shim's resource tenant"
    assert tenants[SPAN_ATTR_TRACE] == "integration", "a span-level tenant is dropped and filled"
    fixture_traces = {s["trace_id"] for fx in FIXTURES for s in flatten_otlp_json(fx.load())}
    assert {tenants[t] for t in fixture_traces} == {"integration"}, "filled from config"
