"""Integration fixtures: a ClickHouse server in a test container, and the built collector.

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

import pytest
from testcontainers.core.container import DockerContainer

from traceblame_storage.testing import ClickHouseServer

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
