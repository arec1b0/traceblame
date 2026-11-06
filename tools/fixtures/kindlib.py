"""Helpers the kind captures share (capture_agentic_rag.py, #12; capture_runbook_agent.py, #13):
processes, git, the kind cluster's API, port-forwards, ClickHouse, and OTLP JSON."""

import base64
import json
import re
import socket
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import capture as common

ROOT = common.ROOT
KUBECTL = ROOT / ".tools" / "bin" / "kubectl"
KUBE_CONTEXT = "kind-traceblame"
NAMESPACE = "observability"


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kwargs)  # noqa: S603


def kubectl(*args: str, input: str | None = None) -> str:
    """kubectl on the kind cluster's context, never the current one."""
    return run([str(KUBECTL), "--context", KUBE_CONTEXT, *args], input=input).stdout


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def is_dirty(repo: Path) -> bool:
    """Whether the checkout has uncommitted changes (untracked files count)."""
    return bool(git(repo, "status", "--porcelain"))


def require_clean(repo: Path) -> str:
    if is_dirty(repo):
        raise SystemExit(f"capture: {repo} has uncommitted changes; provenance needs a commit")
    return git(repo, "rev-parse", "HEAD")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def check_model(ollama: str) -> None:
    pulled = {m["name"]: m["digest"] for m in common.get_json(f"{ollama}/api/tags")["models"]}
    if pulled.get(common.CHAT_MODEL) != common.CHAT_MODEL_DIGEST:
        raise SystemExit(
            f"capture: {common.CHAT_MODEL} on {ollama} is {pulled.get(common.CHAT_MODEL)}, "
            f"pinned {common.CHAT_MODEL_DIGEST}"
        )


def tenant_password(tenant: str) -> str:
    raw = kubectl(
        "-n", "bench", "get", "secret", "tenant-credentials", "-o", f"jsonpath={{.data.{tenant}}}"
    )
    if not raw:
        raise SystemExit(f"capture: no credentials for {tenant}; run `make kind-up`")
    return base64.b64decode(raw).decode()


class PortForward:
    """A short-lived kubectl port-forward to a local free port."""

    def __init__(self, target: str, remote: int, namespace: str = NAMESPACE) -> None:
        self.port = free_port()
        self._proc = subprocess.Popen(  # noqa: S603
            [
                str(KUBECTL),
                "--context",
                KUBE_CONTEXT,
                "-n",
                namespace,
                "port-forward",
                target,
                f"{self.port}:{remote}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.2)
        self.close()
        raise SystemExit(f"capture: port-forward to {target} did not open")

    def close(self) -> None:
        self._proc.terminate()
        self._proc.wait(timeout=10)


def pods(label: str, namespace: str = NAMESPACE) -> list[str]:
    return kubectl(
        "-n", namespace, "get", "pods", "-l", label, "-o", "jsonpath={.items[*].metadata.name}"
    ).split()


def clickhouse(query: str) -> list[dict[str, Any]]:
    pod = pods("clickhouse.altinity.com/chi=traceblame")[0]
    secret = "clickhouse-credentials"  # noqa: S105 - the Secret's name
    user = base64.b64decode(
        kubectl("-n", NAMESPACE, "get", "secret", secret, "-o", "jsonpath={.data.user}")
    ).decode()
    password = base64.b64decode(
        kubectl("-n", NAMESPACE, "get", "secret", secret, "-o", "jsonpath={.data.password}")
    ).decode()
    out = kubectl(
        "-n", NAMESPACE, "exec", pod, "-c", "clickhouse", "--",
        "clickhouse-client", "--user", user, "--password", password, "--database", "traceblame",
        "--query", f"{query} FORMAT JSONEachRow",
    )  # fmt: skip
    return [json.loads(line) for line in out.splitlines() if line]


def stored(trace_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = ",".join(f"'{t}'" for t in sorted(set(trace_ids)) if re.fullmatch(r"[0-9a-f]{32}", t))
    if not ids:
        return []
    # ids are checked hex above, so they cannot carry SQL.
    return clickhouse(f"SELECT * FROM spans WHERE trace_id IN ({ids})")  # noqa: S608


def spans_of(otlp: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (rs, s)
        for rs in otlp["resourceSpans"]
        for ss in rs.get("scopeSpans", [])
        for s in ss.get("spans", [])
    ]


def attr(attrs: Iterable[dict[str, Any]], key: str) -> Any:
    for a in attrs:
        if a["key"] == key:
            value = a["value"]
            if "arrayValue" in value:
                return [next(iter(v.values())) for v in value["arrayValue"].get("values", [])]
            return next(iter(value.values()), None)
    return None


def merge_batches(text: str) -> dict[str, Any]:
    """One OTLP document from the file exporter's JSON lines (one request per line)."""
    batches = [json.loads(line) for line in text.splitlines() if line.strip()]
    return {"resourceSpans": [rs for b in batches for rs in b.get("resourceSpans", [])]}
