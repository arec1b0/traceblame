"""No prompt or completion text reaches any ClickHouse table (issue #11, ADR-0002).

Every golden fixture, content-on ones included, goes through the built collector
(OTLP/HTTP -> normalizer -> stock clickhouse exporter) into a migrated database. Then every
row of every table, in every column, is searched for the capture's text.
"""

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
TABLES = ("spans", "findings", "blame", "injections", "schema_migrations")

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
      exporters: [clickhouse]
"""


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
        for fx in FIXTURES:
            body = fx.path.read_bytes()
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
