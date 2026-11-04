"""Integration fixtures: ClickHouse, Postgres and NATS in test containers, the built collector
and the built trace-assembler.

The container is test-scoped: testcontainers removes it when the session ends (Ryuk). The
image is the one pinned in tools/versions.env. Run with `make integration`, which builds the
collector first.
"""

import os
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from testcontainers.core.container import DockerContainer

from traceblame_storage.testing import ClickHouseServer, NatsServer, PostgresServer

ROOT = Path(__file__).resolve().parents[2]
_USER = "integration"
_PASSWORD = "integration"  # noqa: S105 - throwaway test container


def _versions() -> dict[str, str]:
    lines = (ROOT / "tools" / "versions.env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


@pytest.fixture(scope="session")
def clickhouse() -> Iterator[ClickHouseServer]:
    container = (
        DockerContainer(_versions()["CLICKHOUSE_IMAGE"])
        .with_exposed_ports(8123, 9000)
        .with_env("CLICKHOUSE_USER", _USER)
        .with_env("CLICKHOUSE_PASSWORD", _PASSWORD)
        .with_env("CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1")
    )
    with container:
        server = ClickHouseServer(
            host=container.get_container_host_ip(),
            http_port=int(container.get_exposed_port(8123)),
            native_port=int(container.get_exposed_port(9000)),
            user=_USER,
            password=_PASSWORD,
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                ping = f"http://{server.host}:{server.http_port}/ping"
                with urllib.request.urlopen(ping, timeout=2) as resp:
                    if resp.read().strip() == b"Ok.":
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("ClickHouse did not answer /ping within 60 s")
            time.sleep(0.5)
        yield server


@pytest.fixture
def database(clickhouse: ClickHouseServer) -> Iterator[str]:
    """A fresh database per test."""
    name = f"it_{uuid.uuid4().hex[:12]}"
    yield name
    admin = clickhouse.client("default")
    admin.command(f"DROP DATABASE IF EXISTS `{name}`")
    admin.close()


@pytest.fixture(scope="session")
def otelcol_bin() -> Path:
    path = Path(
        os.environ.get(
            "OTELCOL_BIN", ROOT / "collector" / "_build" / "otelcol" / "otelcol-traceblame"
        )
    )
    if not path.is_file():
        pytest.fail(f"no collector at {path}: run `make collector` (or `make integration`)")
    return path


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresServer]:
    container = (
        DockerContainer(_versions()["POSTGRES_IMAGE"])
        .with_exposed_ports(5432)
        .with_env("POSTGRES_USER", _USER)
        .with_env("POSTGRES_PASSWORD", _PASSWORD)
        .with_env("POSTGRES_DB", "postgres")
    )
    with container:
        server = PostgresServer(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user=_USER,
            password=_PASSWORD,
        )
        # The image starts a throwaway server to run its init, then restarts: ready is the
        # second "ready to accept connections".
        deadline = time.monotonic() + 90
        while True:
            logs = (container.get_logs()[0] + container.get_logs()[1]).decode("utf-8", "replace")
            if logs.count("database system is ready to accept connections") >= 2:
                try:
                    psycopg.connect(server.url("postgres"), connect_timeout=2).close()
                    break
                except psycopg.OperationalError:
                    pass
            if time.monotonic() > deadline:
                pytest.fail("Postgres did not become ready within 90 s")
            time.sleep(0.5)
        yield server


@pytest.fixture
def pg_database(postgres: PostgresServer) -> Iterator[str]:
    """The URL of a fresh, empty database per test."""
    name = f"it_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres.url("postgres"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield postgres.url(name)
    with psycopg.connect(postgres.url("postgres"), autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture(scope="session")
def nats_container() -> Iterator[DockerContainer]:
    container = (
        DockerContainer(_versions()["NATS_IMAGE"])
        .with_command("-js -m 8222")
        .with_exposed_ports(4222, 8222)
    )
    with container:
        host, monitor = container.get_container_host_ip(), int(container.get_exposed_port(8222))
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://{host}:{monitor}/healthz", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("NATS did not answer /healthz within 60 s")
            time.sleep(0.3)
        yield container


@pytest.fixture(scope="session")
def nats(nats_container: DockerContainer) -> NatsServer:
    return NatsServer(
        host=nats_container.get_container_host_ip(),
        port=int(nats_container.get_exposed_port(4222)),
        monitor_port=int(nats_container.get_exposed_port(8222)),
    )


@pytest.fixture(scope="session")
def assembler_bin() -> Path:
    path = Path(
        os.environ.get("ASSEMBLER_BIN", ROOT / "services" / "assembler" / "_build" / "assembler")
    )
    if not path.is_file():
        pytest.fail(f"no assembler at {path}: run `make assembler` (or `make integration`)")
    return path
