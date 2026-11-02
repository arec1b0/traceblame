# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Capture golden fixtures from real runbook-agent runs in a kind sandbox (#13, ADR-0006).

The app (arec1b0/runbook-agent, instrumented with traceblame-ts) runs in-cluster in
runbook-sandbox, as a ServiceAccount whose Role covers that namespace only, in live mode with
its approval gate on. Real faults in the sandbox (tools/fixtures/runbook_agent/scenarios.py)
fire pinned upstream alert rules (deploy/kind/runbook/monitoring/rules.yml) in Prometheus;
Alertmanager posts each task's alert once to the agent, which triages it with qwen2.5:7b on
the host's Ollama and runs the chosen runbook. A scripted approver answers the gate through
the app's own API. The agent exports to a tee collector in the sandbox, which writes every
request to a file and forwards it unchanged to the observability tier, as tenant-eval.

1. Preflight: the sandbox's RBAC and kubeconfig probes; the canary run (a stub LLM whose
   answers carry canaries, alerts with canary labels, a file-only tee): nothing may leak.
2. One tee session per scenario; tasks one at a time, in the seeded order.
3. ClickHouse must hold exactly the spans of the files, as tenant-eval with the eval marker;
   Alertmanager's webhook count and the app's alert count must equal the number of tasks; no
   alert text in the files or in ClickHouse. Report in evals/reports/; fixtures and provenance
   only when every check passes.

Run from WSL2 or Linux after `make kind-up kind-deploy kind-runbook-up`:
`make fixtures-runbook-agent`.
"""

import argparse
import hashlib
import json
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "runbook_agent"))

import capture as common  # noqa: E402 - after the path insert
import rules  # noqa: E402
import scenarios  # noqa: E402
from kindlib import (  # noqa: E402
    PortForward,
    attr,
    check_model,
    kubectl,
    merge_batches,
    require_clean,
    run,
    spans_of,
    stored,
    tenant_password,
)

ROOT = common.ROOT
OUT = common.OUT
REPORTS = ROOT / "evals" / "reports"
SANDBOX = "runbook-sandbox"
MONITORING = "runbook-monitoring"
APP_REPOSITORY = "https://github.com/arec1b0/runbook-agent"
SERVICE = "runbook-agent"
TENANT = "tenant-eval"
MARKER = "aegis.eval.run.id"
AGENT_SA = f"system:serviceaccount:{SANDBOX}:runbook-agent"
KCTL = [str(ROOT / ".tools" / "bin" / "kubectl"), "--context", "kind-traceblame"]
SDK_DIR = ROOT / "sdk" / "ts"
PNPM = ROOT / ".tools" / "bin" / "pnpm"
STUB = HERE / "runbook_agent" / "stub_llm.py"
SANDBOX_TEMPLATES = ROOT / "deploy" / "kind" / "runbook" / "sandbox"
PROM = f"/api/v1/namespaces/{MONITORING}/services/prometheus:9090/proxy"
AM = f"/api/v1/namespaces/{MONITORING}/services/alertmanager:9093/proxy"
# tail_sampling decision_wait (30 s) plus the exporters' flush, with room to spare.
SETTLE_SECONDS = 240
# The shim's batch delay (5 s) and the tee's file flush, before a session's file is read.
FLUSH_SECONDS = 15
OLLAMA_HOST = "host.docker.internal"
OLLAMA_PORT = 11434
OLLAMA_IN_CLUSTER = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/v1"
# A model Ollama does not have: the API-error task's triage call gets a real HTTP 404.
MISSING_MODEL = "traceblame-missing-model:404"
API_ERROR = "api_error"
AI_SDK = {"ai": "4.3.19", "@ai-sdk/openai": "1.3.24", "@traceloop/node-server-sdk": "0.27.0"}

# The RBAC the sandbox must have: (verb, resource, namespace, subresource) -> allowed.
CAN_I = [
    ("get", "pods", SANDBOX, None, True),
    ("get", "pods", SANDBOX, "log", True),
    ("list", "events", SANDBOX, None, True),
    ("patch", "deployments", SANDBOX, None, True),
    ("update", "deployments", SANDBOX, "scale", True),
    ("get", "pods", MONITORING, None, False),
    ("patch", "deployments", MONITORING, None, False),
    ("get", "pods", "observability", None, False),
    ("get", "pods", "kube-system", None, False),
    ("get", "secrets", SANDBOX, None, False),
    ("delete", "pods", SANDBOX, None, False),
    ("delete", "deployments", SANDBOX, None, False),
    ("create", "pods", SANDBOX, "exec", False),
    ("delete", "namespaces", None, None, False),
    ("list", "nodes", None, None, False),
]

# Outside the agent's egress: the tier it must reach only through the tee.
CLICKHOUSE_IN_CLUSTER = "http://clickhouse-traceblame.observability.svc:8123/ping"
# Run with the app image's own Node: exit 0 when the URL answers, 1 otherwise (5 s timeout).
_REACH_JS = (
    "fetch(process.argv[1],{signal:AbortSignal.timeout(5000)})"
    ".then(()=>process.exit(0),()=>process.exit(1))"
)
# The API server as in-cluster clients reach it (the kubernetes Service, the pod's CA and token).
# `kubectl get --raw` has no in-cluster fallback, so it cannot serve as this probe.
_SA = "/var/run/secrets/kubernetes.io/serviceaccount"
_API_JS = (
    "const token = require('node:fs').readFileSync(process.argv[1] + '/token', 'utf8');"
    "fetch('https://kubernetes.default.svc/version',"
    "{headers:{authorization:'Bearer '+token},signal:AbortSignal.timeout(10000)})"
    ".then((r)=>process.exit(r.ok?0:1),()=>process.exit(1))"
)
_RESOLVE_JS = "require('node:dns').lookup(process.argv[1],{family:4},(e,a)=>{console.log(a||'');})"

TEE_CONFIG = """\
extensions:
  basicauth/agent:
    client_auth:
      username: ${env:TEE_TENANT}
      password: ${env:TEE_PASSWORD}
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  file:
    path: /tee/spans.jsonl
    format: json
    flush_interval: 1s
  otlp_http/agent:
    endpoint: http://otel-agent.observability.svc:4318
    auth:
      authenticator: basicauth/agent
    sending_queue:
      enabled: false
service:
  extensions: [basicauth/agent]
  telemetry:
    metrics:
      level: none
    logs:
      level: warn
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [{exporters}]
"""


def log(message: str) -> None:
    print(f"capture: {message}", file=sys.stderr, flush=True)


def wait_until(what: str, check: Any, timeout: float, every: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise SystemExit(f"capture: timed out waiting for {what}")
        time.sleep(every)


def apply(objects: Iterable[dict[str, Any]]) -> None:
    kubectl(
        "apply",
        "-f",
        "-",
        input=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)}),
    )


def template(path: Path, **values: str) -> str:
    return string.Template(path.read_text(encoding="utf-8")).substitute(values)


def configmap(name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": SANDBOX},
        "data": data,
    }


def raw_json(path: str) -> Any:
    return json.loads(kubectl("get", "--raw", path))


# Build ------------------------------------------------------------------------------------


def build_image(
    app_dir: Path, app_commit: str, traceblame_commit: str, work: Path
) -> dict[str, Any]:
    """The app image at its commit, with the shim packed from this tree at its commit."""
    run([str(PNPM), "-C", str(SDK_DIR), "install", "--frozen-lockfile"])
    run([str(PNPM), "-C", str(SDK_DIR), "build"])
    run([str(PNPM), "-C", str(SDK_DIR), "pack", "--pack-destination", str(work)])
    tarball = next(work.glob("traceblame-ts-*.tgz"))
    listing = run(["tar", "-tzf", str(tarball)]).stdout.split()
    if "package/dist/index.js" not in listing:
        raise SystemExit("capture: the packed shim has no dist/index.js")
    context = work / "app"
    context.mkdir()
    archive = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(app_dir), "archive", app_commit],  # noqa: S607
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-x", "-C", str(context)], input=archive.stdout, check=True)  # noqa: S603, S607
    shutil.copy(tarball, context / tarball.name)
    package = json.loads((context / "package.json").read_text(encoding="utf-8"))
    package.setdefault("optionalDependencies", {})["traceblame-ts"] = f"file:./{tarball.name}"
    (context / "package.json").write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")
    run([str(PNPM), "install", "--lockfile-only", "--ignore-scripts"], cwd=context)
    # The Dockerfile installs from package.json and the lockfile alone, before the rest of the
    # context; without the tarball there pnpm skips the optional dependency silently.
    dockerfile = context / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    if text.count(DOCKERFILE_COPY) != 1:
        raise SystemExit(f"capture: the app's Dockerfile has no single line {DOCKERFILE_COPY!r}")
    copy = DOCKERFILE_COPY.replace(" ./", f" {tarball.name} ./")
    dockerfile.write_text(text.replace(DOCKERFILE_COPY, copy), encoding="utf-8")
    tag = f"runbook-agent:{app_commit[:7]}-tb{traceblame_commit[:7]}"
    kubectl_version = _pins()["KUBECTL_VERSION"]
    run(
        [
            "docker",
            "build",
            "--quiet",
            "--build-arg",
            f"KUBECTL_VERSION=v{kubectl_version}",
            "--tag",
            tag,
            str(context),
        ]
    )
    # The Dockerfile downloads kubectl without a checksum: compare it with ours. Its mode is 0711,
    # so only root can read it; the check runs as root and only reads.
    image_kubectl = run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "--entrypoint",
            "sha256sum",
            tag,
            "/usr/local/bin/kubectl",
        ]
    ).stdout.split()[0]
    want = _checksum(f"kubectl-{kubectl_version}-linux-amd64")
    if image_kubectl != want:
        raise SystemExit(f"capture: the image's kubectl is {image_kubectl}, pinned {want}")
    # The app imports the shim dynamically and runs untraced without it: require it here.
    run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "node",
            tag,
            "--input-type=module",
            "-e",
            _LOADS_JS,
        ]
    )
    image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag]).stdout.strip()
    run(
        [str(ROOT / ".tools" / "bin" / "kind"), "load", "docker-image", "--name", "traceblame", tag]
    )
    installed = json.loads(
        run(["docker", "run", "--rm", "--entrypoint", "node", tag, "-e", _VERSIONS_JS]).stdout
    )
    node = run(["docker", "run", "--rm", "--entrypoint", "node", tag, "--version"]).stdout.strip()
    uid, gid = (
        int(v)
        for v in run(
            ["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", "id -u; id -g"]
        ).stdout.split()
    )
    if uid == 0:
        raise SystemExit("capture: the app image runs as root")
    return {
        "image": tag,
        "image_id": image_id,
        "node": node,
        "kubectl": {"version": kubectl_version, "sha256": image_kubectl},
        "shim_tarball_sha256": hashlib.sha256(tarball.read_bytes()).hexdigest(),
        "dockerfile_patch": {"original": DOCKERFILE_COPY, "used": copy},
        "user": {"uid": uid, "gid": gid},
        "packages": installed,
    }


DOCKERFILE_COPY = "COPY package.json pnpm-lock.yaml ./"
_LOADS_JS = (
    "const m = await import('traceblame-ts');"
    "if (typeof m.instrument !== 'function') throw new Error('traceblame-ts has no instrument');"
)
# The installed versions, read from package.json files directly: an exports map can hide
# `<name>/package.json` from require.resolve, and pnpm keeps the shim's dependencies beside its
# real directory (.pnpm/<id>/node_modules), not under the /app/node_modules symlink.
_VERSIONS_JS = (
    "const fs = require('node:fs'), path = require('node:path');"
    "const shim = fs.realpathSync('/app/node_modules/traceblame-ts');"
    "const roots = [path.dirname(shim), '/app/node_modules'];"
    "const names = ['ai', '@ai-sdk/openai', 'traceblame-ts', '@traceloop/node-server-sdk',"
    " '@opentelemetry/sdk-trace-base', '@opentelemetry/api'];"
    "const out = {}; for (const n of names) {"
    " const file = roots.map((r) => path.join(r, n, 'package.json')).find(fs.existsSync);"
    " if (!file) throw new Error('not installed: ' + n);"
    " out[n] = JSON.parse(fs.readFileSync(file, 'utf8')).version; }"
    "process.stdout.write(JSON.stringify(out));"
)


# Sandbox ----------------------------------------------------------------------------------


def deploy_tee(session: str, *, forward: bool, password: str) -> None:
    exporters = "file, otlp_http/agent" if forward else "file"
    apply(
        [
            configmap("tee-config", {"tee.yaml": TEE_CONFIG.replace("{exporters}", exporters)}),
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "tee-tenant", "namespace": SANDBOX},
                "stringData": {"username": TENANT, "password": password},
            },
        ]
    )
    pins = _pins()
    kubectl(
        "apply",
        "-f",
        "-",
        input=template(
            SANDBOX_TEMPLATES / "tee.yaml",
            OTELCOL_CONTRIB_IMAGE=pins["OTELCOL_CONTRIB_IMAGE"],
            BUSYBOX_IMAGE=pins["BUSYBOX_IMAGE"],
        ),
    )
    # A fresh pod, so the session's file holds this session only.
    kubectl("-n", SANDBOX, "rollout", "restart", "deployment/tee")
    kubectl("-n", SANDBOX, "rollout", "status", "deployment/tee", "--timeout=300s")
    log(f"tee session {session} ({'file and forward' if forward else 'file only'})")


def read_tee() -> dict[str, Any]:
    text = kubectl(
        "-n", SANDBOX, "exec", "deploy/tee", "-c", "reader", "--", "cat", "/tee/spans.jsonl"
    )
    return merge_batches(text)


def deploy_agent(build: dict[str, Any], env: dict[str, str]) -> None:
    apply([configmap("runbook-agent-env", env)])
    objects = list(
        yaml.safe_load_all(
            template(SANDBOX_TEMPLATES / "agent.yaml", RUNBOOK_AGENT_IMAGE=build["image"])
        )
    )
    # runAsNonRoot needs a numeric user, and the app's Dockerfile names one (USER agent): run as
    # that user's ids, read from the image.
    for obj in objects:
        if obj["kind"] == "Deployment":
            obj["spec"]["template"]["spec"]["securityContext"] |= {
                "runAsUser": build["user"]["uid"],
                "runAsGroup": build["user"]["gid"],
            }
    apply(objects)
    kubectl("-n", SANDBOX, "rollout", "restart", "deployment/runbook-agent")
    kubectl("-n", SANDBOX, "rollout", "status", "deployment/runbook-agent", "--timeout=300s")


def _checksum(file: str) -> str:
    for line in (ROOT / "tools" / "checksums.txt").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if name == file:
            return digest
    raise SystemExit(f"capture: no checksum for {file}")


def _pins() -> dict[str, str]:
    text = (ROOT / "tools" / "versions.env").read_text(encoding="utf-8")
    return dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
    )


def safety_probes() -> list[dict[str, Any]]:
    """The agent's reach, from the API server's view and from inside its pod."""
    out: list[dict[str, Any]] = []
    for verb, resource, namespace, sub, want in CAN_I:
        args = ["auth", "can-i", verb, resource, f"--as={AGENT_SA}"]
        args += ["-n", namespace] if namespace else []
        args += [f"--subresource={sub}"] if sub else []
        got = (
            subprocess.run(  # noqa: S603 - fixed argv
                [*KCTL, *args],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            == "yes"
        )
        out.append(
            {
                "check": " ".join(args[2:4])
                + (f"/{sub}" if sub else "")
                + f" in {namespace or 'cluster'}",
                "allowed": got,
                "ok": got == want,
            }
        )

    def in_pod(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv
            [*KCTL, "-n", SANDBOX, "exec", "deploy/runbook-agent", "--", *argv],
            capture_output=True,
            text=True,
            check=False,
        )

    # The egress the network policy allows, as applied, against the cluster as it is now:
    # kind-runbook-up resolved the addresses, and a recreated cluster can move them.
    policy = json.loads(
        kubectl("-n", SANDBOX, "get", "networkpolicy", "runbook-agent", "-o", "json")
    )
    allowed = {
        (block["ipBlock"]["cidr"], port["port"])
        for rule in policy["spec"]["egress"]
        for block in rule.get("to", [])
        if "ipBlock" in block
        for port in rule.get("ports", [])
    }
    slice_ = json.loads(
        kubectl(
            "-n",
            "default",
            "get",
            "endpointslice",
            "-l",
            "kubernetes.io/service-name=kubernetes",
            "-o",
            "json",
        )
    )["items"][0]
    api = {
        (f"{address}/32", port["port"])
        for endpoint in slice_["endpoints"]
        for address in endpoint["addresses"]
        for port in slice_["ports"]
        if port.get("name") == "https"
    }
    out.append(
        {
            "check": "network policy: API server egress is the live EndpointSlice",
            "allowed": api <= allowed,
            "ok": bool(api) and api <= allowed,
        }
    )
    resolved = in_pod("node", "-e", _RESOLVE_JS, OLLAMA_HOST).stdout.strip()
    out.append(
        {
            "check": f"network policy: Ollama egress is {OLLAMA_HOST} as resolved in the pod",
            "allowed": (f"{resolved}/32", OLLAMA_PORT) in allowed,
            "ok": bool(resolved) and (f"{resolved}/32", OLLAMA_PORT) in allowed,
        }
    )
    version = in_pod("env", f"NODE_EXTRA_CA_CERTS={_SA}/ca.crt", "node", "-e", _API_JS, _SA)
    out.append(
        {
            "check": "in pod: API server reachable (GET /version)",
            "allowed": version.returncode == 0,
            "ok": version.returncode == 0,
        }
    )
    ollama = in_pod("node", "-e", _REACH_JS, f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/version")
    out.append(
        {
            "check": "in pod: Ollama reachable",
            "allowed": ollama.returncode == 0,
            "ok": ollama.returncode == 0,
        }
    )
    clickhouse = in_pod("node", "-e", _REACH_JS, CLICKHOUSE_IN_CLUSTER)
    out.append(
        {
            "check": "in pod: ClickHouse unreachable (egress outside the policy)",
            "allowed": clickhouse.returncode == 0,
            "ok": clickhouse.returncode != 0,
        }
    )
    monitoring = in_pod("kubectl", "get", "pods", "-n", MONITORING)
    out.append(
        {
            "check": "in pod: kubectl get pods -n runbook-monitoring",
            "allowed": monitoring.returncode == 0,
            "ok": monitoring.returncode != 0 and "Forbidden" in monitoring.stderr,
        }
    )
    sandbox = in_pod("kubectl", "get", "pods", "-n", SANDBOX)
    out.append(
        {
            "check": "in pod: kubectl get pods -n runbook-sandbox",
            "allowed": sandbox.returncode == 0,
            "ok": sandbox.returncode == 0,
        }
    )
    contexts = in_pod("kubectl", "config", "get-contexts", "-o", "name")
    out.append(
        {
            "check": "in pod: kubeconfig contexts",
            "allowed": bool(contexts.stdout.strip()),
            "ok": contexts.stdout.strip() == "",
        }
    )
    files = in_pod(
        "sh", "-c", "ls ${KUBECONFIG:-$HOME/.kube/config} /root/.kube/config 2>/dev/null; true"
    )
    out.append(
        {
            "check": "in pod: kubeconfig files",
            "allowed": bool(files.stdout.strip()),
            "ok": files.stdout.strip() == "",
        }
    )
    return out


class Agent:
    """The app's own HTTP API through a port-forward, as a human operator would use it."""

    def __init__(self) -> None:
        self._forward = PortForward("svc/runbook-agent", 3000, SANDBOX)
        self.base = f"http://127.0.0.1:{self._forward.port}"

    def get(self, path: str) -> Any:
        with urllib.request.urlopen(f"{self.base}{path}", timeout=10) as r:  # noqa: S310 - local
            body = r.read().decode()
        return json.loads(body) if path != "/metrics" else body

    def post(self, path: str, payload: Any) -> Any:
        req = urllib.request.Request(  # noqa: S310 - local
            f"{self.base}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return json.loads(r.read().decode())

    def incidents(self) -> int:
        return int(self.get("/healthz")["incidents"])

    def alerts_received(self) -> float:
        for line in self.get("/metrics").splitlines():
            if line.startswith("alerts_received_total "):
                return float(line.split()[1])
        return 0.0

    def close(self) -> None:
        self._forward.close()


def last_incident() -> dict[str, Any]:
    lines = kubectl(
        "-n", SANDBOX, "exec", "deploy/runbook-agent", "--", "cat", "/app/data/incidents.jsonl"
    ).splitlines()
    return json.loads(lines[-1])


def webhook_requests() -> tuple[float, float]:
    """Alertmanager's webhook HTTP requests and failed ones. Not alertmanager_notifications_total:
    that counts every flush of a group, including the resolved-only flushes that send nothing
    when send_resolved is false (notify/retry_stage.go)."""
    totals = {
        "alertmanager_notification_requests_total": 0.0,
        "alertmanager_notification_requests_failed_total": 0.0,
    }
    for line in kubectl("get", "--raw", f"{AM}/metrics").splitlines():
        name = line.split("{", 1)[0]
        if name in totals and 'integration="webhook"' in line:
            totals[name] += float(line.split()[-1])
    return (
        totals["alertmanager_notification_requests_total"],
        totals["alertmanager_notification_requests_failed_total"],
    )


def active_alerts(service: str) -> list[str]:
    alerts = raw_json(f"{PROM}/api/v1/alerts")["data"]["alerts"]
    return [a["labels"]["alertname"] for a in alerts if a["labels"].get("service") == service]


@contextmanager
def stub_llm(canary: str) -> Iterator[None]:
    pins = _pins()
    apply(
        [
            configmap("stub-llm", {"stub_llm.py": STUB.read_text(encoding="utf-8")}),
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "stub-llm",
                    "namespace": SANDBOX,
                    "labels": {"app": "stub-llm"},
                },
                "spec": {
                    "serviceAccountName": "workload",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "stub-llm",
                            "image": pins["PYTHON_IMAGE"],
                            "command": ["python", "/stub/stub_llm.py", canary],
                            "ports": [{"containerPort": 8080}],
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 65534,
                                "allowPrivilegeEscalation": False,
                            },
                            "volumeMounts": [{"name": "stub", "mountPath": "/stub"}],
                        }
                    ],
                    "volumes": [{"name": "stub", "configMap": {"name": "stub-llm"}}],
                },
            },
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "stub-llm", "namespace": SANDBOX},
                "spec": {
                    "selector": {"app": "stub-llm"},
                    "ports": [{"port": 8080, "targetPort": 8080}],
                },
            },
        ]
    )
    kubectl("-n", SANDBOX, "wait", "pod/stub-llm", "--for=condition=Ready", "--timeout=180s")
    try:
        yield
    finally:
        kubectl(
            "-n",
            SANDBOX,
            "delete",
            "pod/stub-llm",
            "service/stub-llm",
            "configmap/stub-llm",
            "--wait=false",
        )


# Checks -----------------------------------------------------------------------------------


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():  # pyright: ignore[reportUnknownVariableType]
            yield str(k)  # pyright: ignore[reportUnknownArgumentType]
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:  # pyright: ignore[reportUnknownVariableType]
            yield from strings(v)


def leaks(needles: Iterable[str], *payloads: Any) -> list[str]:
    """The needles found in any string of the payloads (an alert's text, label values)."""
    haystack = "\x00".join(s for p in payloads for s in strings(p))
    return sorted({n for n in needles if n and n in haystack})


def alert_needles(incident: dict[str, Any]) -> set[str]:
    """What an alert put into the agent's prompt and tool arguments, beyond fixed vocabulary:
    its summary in 32-character windows, and its label values unique to the task."""
    alert = incident["alert"]
    out: set[str] = set()
    summary = alert.get("summary") or ""
    out |= {summary[i : i + 32] for i in range(0, max(len(summary) - 32, 0) + 1, 8)}
    for key, value in alert["labels"].items():
        if (
            key in ("pod", "container", "service", "deployment", "uid", "instance")
            and len(value) >= 5
        ):
            out.add(value)
    return out


def check_canary(otlp: dict[str, Any], canary: str) -> list[str]:
    problems: list[str] = []
    if canary in json.dumps(otlp):
        problems.append("a canary reached the export")
    llm = [
        s
        for _, s in spans_of(otlp)
        if attr(s.get("attributes", []), "ai.operationId") == "ai.generateObject.doGenerate"
    ]
    parse = [attr(s["attributes"], "aegis.output.parse_ok") for s in llm]
    if True not in parse or False not in parse:
        problems.append(f"parse_ok values on the LLM spans: {parse}")
    if 400 not in [int(attr(s["attributes"], "http.response.status_code") or 0) for s in llm]:
        problems.append("no LLM span with http.response.status_code 400")
    tools = [
        s
        for _, s in spans_of(otlp)
        if attr(s.get("attributes", []), "openinference.span.kind") == "TOOL"
    ]
    if not tools or not all(attr(s["attributes"], "aegis.tool.args_hash") for s in tools):
        problems.append("tool spans without aegis.tool.args_hash")
    return problems


def check_api_error(otlp: dict[str, Any]) -> list[str]:
    """The API-error session: every LLM span is the failed call, with the status the server
    sent and the AI SDK's error type, and without an output feature."""
    llm = [
        s
        for _, s in spans_of(otlp)
        if attr(s.get("attributes", []), "ai.operationId") == "ai.generateObject.doGenerate"
    ]
    if not llm:
        return ["no LLM span"]
    problems: list[str] = []
    for s in llm:
        status = attr(s["attributes"], "http.response.status_code")
        types = [
            attr(e.get("attributes", []), "exception.type")
            for e in s.get("events", [])
            if e.get("name") == "exception"
        ]
        if int(status or 0) != 404 or types != ["AI_APICallError"]:
            problems.append(f"LLM span: status {status}, exception types {types}")
        if attr(s["attributes"], "aegis.output.parse_ok") is not None:
            problems.append("LLM span of a failed call with aegis.output.parse_ok")
    return problems


# Tasks ------------------------------------------------------------------------------------


def run_task(task: Any, agent: Agent, image: str, approval_timeout_s: float) -> dict[str, Any]:
    started = datetime.now(UTC)
    received, (requests, failed), incidents = (
        agent.alerts_received(),
        webhook_requests(),
        agent.incidents(),
    )
    if task.scenario == "oom":
        # The rule sees only pods at least ten minutes old (scenarios.OOM_MIN_AGE_SECONDS).
        pod = json.loads(
            kubectl("-n", SANDBOX, "get", "pods", "-l", f"app={task.name}", "-o", "json")
        )["items"][0]
        born = datetime.fromisoformat(pod["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
        wait = scenarios.OOM_MIN_AGE_SECONDS - (datetime.now(UTC) - born).total_seconds()
        if wait > 0:
            log(f"{task.name}: waiting {wait:.0f} s for the pod to age")
            time.sleep(wait)
        kubectl(
            "-n",
            SANDBOX,
            "exec",
            f"deploy/{task.name}",
            "-c",
            task.name,
            "--",
            "touch",
            scenarios.TRIGGER,
        )
    else:
        apply(scenarios.manifests(task, _pins()["PYTHON_IMAGE"]))
    wait_until(f"{task.name}'s webhook", lambda: agent.alerts_received() > received, 600, 3)
    approvals: list[dict[str, Any]] = []
    seen: set[str] = set()
    deadline = time.monotonic() + approval_timeout_s + 600
    while agent.incidents() == incidents:
        for pending in agent.get("/approvals")["pending"]:
            if pending["id"] in seen:
                continue
            seen.add(pending["id"])
            decision = task.approval or "approve"
            if decision != "timeout":
                agent.post(f"/approvals/{pending['id']}", {"approved": decision == "approve"})
            approvals.append({"action": pending["actionId"], "decision": decision})
        if time.monotonic() > deadline:
            raise SystemExit(f"capture: {task.name}: no incident recorded")
        time.sleep(1)
    incident = last_incident()
    after_received, (after_requests, after_failed) = agent.alerts_received(), webhook_requests()
    for obj in reversed(scenarios.manifests(task, "unused")):
        kubectl(
            "-n",
            SANDBOX,
            "delete",
            obj["kind"].lower(),
            task.name,
            "--ignore-not-found",
            "--wait=true",
        )
    wait_until(f"{task.name}'s alerts to clear", lambda: not active_alerts(task.name), 600, 5)
    return {
        "task": task.name,
        "scenario": task.scenario,
        "order": task.order,
        "params": task.params,
        "alert": incident["alert"]["name"],
        "expected_runbook": task.expected_runbook,
        "runbook": incident["runbookId"] or "none",
        "status": incident["status"],
        "steps": [{"action": s["actionId"], "status": s["status"]} for s in incident["steps"]],
        "approval_policy": task.approval,
        "approvals": approvals,
        "alerts_received": int(after_received - received),
        "webhooks": int(after_requests - requests),
        "webhooks_failed": int(after_failed - failed),
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_needles": sorted(alert_needles(incident)),
    }


# Main -------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument(
        "--ollama-url", help="for the model check; default: localhost, then the WSL2 host"
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--per-scenario", type=int, default=6)
    parser.add_argument("--approval-timeout-ms", type=int, default=30_000)
    args = parser.parse_args()

    app_dir: Path = args.app_dir.resolve()
    traceblame_commit = require_clean(ROOT)
    app_commit = require_clean(app_dir)
    ollama = common.resolve_ollama(args.ollama_url)
    server_version = common.get_json(f"{ollama}/api/version")["version"]
    check_model(ollama)
    if MISSING_MODEL in {m["name"] for m in common.get_json(f"{ollama}/api/tags")["models"]}:
        raise SystemExit(
            f"capture: {MISSING_MODEL} exists on {ollama}; the 404 task needs it absent"
        )
    password = tenant_password(TENANT)
    tenants = (
        yaml.safe_load(
            kubectl(
                "-n",
                "observability",
                "get",
                "configmap",
                "eval-tenants",
                "-o",
                "jsonpath={.data.TRACEBLAME_EVAL_TENANTS}",
            )
        )
        or []
    )
    if TENANT not in tenants:
        raise SystemExit(f"capture: eval_tenants is {tenants}; want {TENANT}")
    rule_provenance = rules.provenance()
    plan = scenarios.plan(args.seed, args.per_scenario)
    api_task = scenarios.api_error_task(args.seed, args.per_scenario)
    day = datetime.now(UTC).strftime("%Y%m%d")
    run_id = f"rb-{day}-s{args.seed}-{traceblame_commit[:7]}"
    canary = "CANARY-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    report: dict[str, Any] = {
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "traceblame_commit": traceblame_commit,
        "app": {"repository": APP_REPOSITORY, "commit": app_commit},
        "eval_run_id": run_id,
        "tenant": TENANT,
        "problems": [],
    }
    problems: list[str] = report["problems"]
    base_env = {
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "ollama",
        "EXECUTION_MODE": "live",
        "APPROVAL_TIMEOUT_MS": str(args.approval_timeout_ms),
        "LOG_LEVEL": "warn",
        "TRACEBLAME_ENABLED": "1",
        "TRACEBLAME_TENANT_ID": TENANT,
        "TRACEBLAME_EVAL_RUN_ID": run_id,
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://tee.{SANDBOX}.svc:4318",
    }

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        log("building the agent image")
        build = build_image(app_dir, app_commit, traceblame_commit, work)
        report["build"] = build
        # A clean slate: no leftover workloads, Alertmanager without earlier groups.
        kubectl(
            "-n",
            SANDBOX,
            "delete",
            "deployment,service,pod",
            "-l",
            f"{scenarios.LABEL}=workload",
            "--wait=true",
        )
        kubectl("-n", MONITORING, "rollout", "restart", "deployment/alertmanager")
        kubectl("-n", MONITORING, "rollout", "status", "deployment/alertmanager", "--timeout=300s")

        # 1. Canary preflight: stub LLM, canary alerts posted straight to the webhook, file only.
        log("canary preflight")
        deploy_tee("canary", forward=False, password=password)
        with stub_llm(canary):
            deploy_agent(
                build,
                base_env
                | {
                    "LLM_MODEL": "stub",
                    "LLM_BASE_URL": f"http://stub-llm.{SANDBOX}.svc:8080/v1",
                    "LLM_STRUCTURED_OUTPUTS": "true",
                    "TRACEBLAME_EVAL_RUN_ID": f"{run_id}-canary",
                },
            )
            report["safety"] = safety_probes()
            failed = [p["check"] for p in report["safety"] if not p["ok"]]
            if failed:
                raise SystemExit(f"capture: safety probes failed: {failed}")
            agent = Agent()
            try:
                for i in range(3):
                    before = agent.incidents()
                    payload = {
                        "version": "4",
                        "groupKey": f"canary-{i}",
                        "status": "firing",
                        "receiver": "capture",
                        "alerts": [
                            {
                                "status": "firing",
                                "labels": {
                                    "alertname": "OOMKilled",
                                    "severity": "critical",
                                    "service": f"{canary.lower()}-{i}",
                                    "namespace": SANDBOX,
                                },
                                "annotations": {"summary": f"{canary} summary {i}"},
                                "startsAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            }
                        ],
                    }
                    agent.post("/webhook/alertmanager", payload)
                    while agent.incidents() == before:
                        for pending in agent.get("/approvals")["pending"]:
                            agent.post(f"/approvals/{pending['id']}", {"approved": False})
                        time.sleep(1)
            finally:
                agent.close()
            time.sleep(FLUSH_SECONDS)
            canary_otlp = read_tee()
        canary_problems = check_canary(canary_otlp, canary)
        report["canary"] = {"spans": len(spans_of(canary_otlp)), "problems": canary_problems}
        if canary_problems:
            raise SystemExit(f"capture: canary preflight failed: {canary_problems}")

        # 2. Real runs: the agent on Ollama; oom workloads start now, so they are old enough.
        env = base_env | {
            "LLM_MODEL": common.CHAT_MODEL,
            "LLM_BASE_URL": OLLAMA_IN_CLUSTER,
            "LLM_STRUCTURED_OUTPUTS": "true",
        }
        deploy_agent(build, env)
        report["safety_after_restart"] = safety_probes()
        if not all(p["ok"] for p in report["safety_after_restart"]):
            raise SystemExit("capture: safety probes failed after the restart")
        pre = [
            o
            for t in plan
            if t.scenario == "oom"
            for o in scenarios.manifests(t, _pins()["PYTHON_IMAGE"])
        ]
        names = scenarios.check_names(
            [o for t in [*plan, api_task] for o in scenarios.manifests(t, _pins()["PYTHON_IMAGE"])]
        )
        if names:
            raise SystemExit(f"capture: workload names: {names}")
        apply(pre)
        sessions: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        agent = Agent()
        try:
            for scenario in dict.fromkeys(t.scenario for t in plan):
                deploy_tee(scenario, forward=True, password=password)
                for task in [t for t in plan if t.scenario == scenario]:
                    log(f"task {task.order + 1}/{len(plan) + 1}: {task.name}")
                    results.append(
                        run_task(task, agent, build["image"], args.approval_timeout_ms / 1000)
                        | {"session": scenario}
                    )
                time.sleep(FLUSH_SECONDS)
                sessions[scenario] = read_tee()
        finally:
            agent.close()

        # One more real task, with a model the server does not have: a real HTTP 404.
        envs = dict.fromkeys(sessions, env)
        envs[API_ERROR] = env | {"LLM_MODEL": MISSING_MODEL}
        deploy_agent(build, envs[API_ERROR])
        report["safety_api_error"] = safety_probes()
        if not all(p["ok"] for p in report["safety_api_error"]):
            raise SystemExit("capture: safety probes failed after the restart")
        deploy_tee(API_ERROR, forward=True, password=password)
        agent = Agent()
        try:
            log(f"task {api_task.order + 1}/{len(plan) + 1}: {api_task.name} ({MISSING_MODEL})")
            results.append(
                run_task(api_task, agent, build["image"], args.approval_timeout_ms / 1000)
                | {"session": API_ERROR}
            )
        finally:
            agent.close()
        time.sleep(FLUSH_SECONDS)
        sessions[API_ERROR] = read_tee()
        report["api_error"] = {
            "model": MISSING_MODEL,
            "problems": check_api_error(sessions[API_ERROR]),
        }
        problems += report["api_error"]["problems"]

        # 3. ClickHouse, webhooks, content.
        sent = {
            (s["traceId"], s["spanId"]) for otlp in sessions.values() for _, s in spans_of(otlp)
        }
        rows = wait_until(
            "ClickHouse to hold every span",
            lambda: (r := stored(t for t, _ in sent)) and len(r) >= len(sent) and r,
            SETTLE_SECONDS,
            10,
        )
        time.sleep(15)
        rows = stored(t for t, _ in sent)
        got = {(r["trace_id"], r["span_id"]) for r in rows}
        if got != sent or len(rows) != len(sent):
            problems.append(
                f"{len(sent)} spans sent, {len(rows)} rows stored, "
                f"{len(sent - got)} missing, {len(got - sent)} unexpected"
            )
        if any(r["tenant_id"] != TENANT for r in rows):
            problems.append("a row with another tenant")
        if any(r["resource_attrs"].get(MARKER) != run_id for r in rows):
            problems.append("a row without the eval run marker")
        webhooks = sum(r["webhooks"] for r in results)
        received = sum(r["alerts_received"] for r in results)
        failed = sum(r["webhooks_failed"] for r in results)
        if not (
            webhooks == received == len(results)
            and failed == 0
            and all(r["webhooks"] == 1 for r in results)
        ):
            problems.append(
                f"{len(results)} tasks, {webhooks} webhook requests ({failed} failed), "
                f"{received} alerts received"
            )
        needles = {n for r in results for n in r.pop("_needles")}
        found = leaks(needles, list(sessions.values()), rows)
        report["content_check"] = {"needles": len(needles), "found": len(found)}
        if found:
            problems.append(f"{len(found)} alert strings found in the exports")
        by_scenario = {
            s: {
                "tasks": sum(r["session"] == s for r in results),
                "expected_runbook": next(
                    r["expected_runbook"] for r in results if r["session"] == s
                ),
                "runbooks": dict(Counter(r["runbook"] for r in results if r["session"] == s)),
                "statuses": dict(Counter(r["status"] for r in results if r["session"] == s)),
                "spans": len(spans_of(sessions[s])),
                "traces": len({sp["traceId"] for _, sp in spans_of(sessions[s])}),
            }
            for s in sessions
        }
        report["scenarios"] = by_scenario
        report["runbooks"] = dict(Counter(r["runbook"] for r in results))
        report["missed_runbook"] = [
            {"task": r["task"], "expected": r["expected_runbook"], "got": r["runbook"]}
            for r in results
            if r["runbook"] != r["expected_runbook"]
        ]
        report["tasks"] = results
        report["webhooks"] = {
            "tasks": len(results),
            "webhooks": webhooks,
            "webhooks_failed": failed,
            "alerts_received": received,
        }
        report["spans"] = {"sent": len(sent), "stored": len(rows)}

    stem = f"sdk-ts-kind-{datetime.now(UTC).strftime('%Y-%m-%d')}"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (REPORTS / f"{stem}.md").write_text(markdown(report), encoding="utf-8")
    log(f"wrote evals/reports/{stem}.md")
    if problems:
        raise SystemExit("capture: checks failed, no fixtures written:\n  " + "\n  ".join(problems))

    # 4. Fixtures and provenance.
    target = (
        OUT / "openllmetry" / f"traceloop-node-server-sdk-{AI_SDK['@traceloop/node-server-sdk']}"
    )
    target.mkdir(parents=True, exist_ok=True)
    pins = _pins()
    for scenario, otlp in sessions.items():
        name = f"runbook_{scenario}"
        fixture = target / f"{name}.otlp.json"
        fixture.write_text(json.dumps(otlp, indent=2) + "\n", encoding="utf-8")
        tasks = [r for r in results if r["session"] == scenario]
        provenance = {
            "sdk": "openllmetry",
            "scenario": name,
            "captured_at": report["captured_at"],
            "capture_script": "tools/fixtures/capture_runbook_agent.py",
            "capture_script_sha256": common.sha256(Path(__file__)),
            "node": build["node"],
            "pnpm": _pins()["PNPM_VERSION"],
            "packages": build["packages"],
            "env": {k: v for k, v in envs[scenario].items() if k != "OPENAI_API_KEY"},
            "content_capture": False,
            "content_check": "passed",
            "model": {
                "name": common.CHAT_MODEL,
                "digest": common.CHAT_MODEL_DIGEST,
                "server": "ollama",
                "server_version": server_version,
            },
            "endpoint": OLLAMA_IN_CLUSTER,
            "collector": f"otelcol-contrib {pins['OTELCOL_CONTRIB_IMAGE']}",
            "fixture_sha256": common.sha256(fixture),
            "app": {"repository": APP_REPOSITORY, "commit": app_commit},
            "shim": {
                "package": "traceblame-ts",
                "version": build["packages"]["traceblame-ts"],
                "commit": traceblame_commit,
            },
            "run": {
                "report": f"evals/reports/{stem}.md",
                "eval_run_id": run_id,
                "tenant": TENANT,
                "image": build["image"],
                "image_id": build["image_id"],
            },
            "scenario_run": {
                "seed": args.seed,
                "per_scenario": args.per_scenario,
                "alert": scenarios.ALERT[tasks[0]["scenario"]],
                "expected_runbook": tasks[0]["expected_runbook"],
                "rules": rule_provenance,
                "approver": f"scripted:{args.seed}",
                "approval_timeout_ms": args.approval_timeout_ms,
                "tasks": tasks,
            },
            "monitoring": {
                k: pins[k]
                for k in (
                    "PROMETHEUS_IMAGE",
                    "ALERTMANAGER_IMAGE",
                    "KUBE_STATE_METRICS_IMAGE",
                    "BLACKBOX_EXPORTER_IMAGE",
                    "PYTHON_IMAGE",
                )
            },
            "notes": [
                "Real faults in the runbook-sandbox namespace fire pinned upstream alert rules; "
                "Alertmanager posts each task's alert once to the agent.",
                "The agent runs in-cluster with a namespace-only Role, in live mode with its "
                "approval gate answered by a scripted approver through the app's own API.",
                "The image is the app's Dockerfile at its commit, built from a context with the "
                "packed traceblame-ts: package.json names the tarball as an optional dependency, "
                "and the Dockerfile's dependency COPY includes it.",
                "Exported through traceblame-ts: AI SDK telemetry with recordInputs and "
                "recordOutputs off (ADR-0006). The file is what the tee collector received; "
                "the kind agent got the same requests.",
                *(
                    [
                        f"The agent's model was {MISSING_MODEL}, which the server does not "
                        "have: the triage call failed with a real HTTP 404 and the app declined "
                        "to remediate."
                    ]
                    if scenario == API_ERROR
                    else []
                ),
            ],
        }
        fixture.with_name(f"{name}.provenance.yaml").write_text(
            yaml.safe_dump(provenance, sort_keys=False, width=100), encoding="utf-8"
        )
        log(f"wrote {fixture.relative_to(ROOT)}")
    return 0


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# traceblame-ts on runbook-agent, kind ({report['captured_at']})",
        "",
        f"- traceblame `{report['traceblame_commit']}`, app `{report['app']['commit']}`",
        f"- eval run `{report['eval_run_id']}`, tenant `{report['tenant']}`",
        f"- canary: {report['canary']['spans']} spans, "
        f"{report['canary']['problems'] or 'no problems'}",
        f"- safety probes: {sum(p['ok'] for p in report['safety'])}/{len(report['safety'])} passed",
        f"- webhooks: {report['webhooks']}",
        f"- API error task ({report['api_error']['model']}): "
        f"{report['api_error']['problems'] or 'no problems'}",
        f"- spans: {report['spans']}",
        f"- content check: {report['content_check']}",
        "",
        "| Scenario | Tasks | Expected runbook | Runbooks | Statuses | Spans | Traces |",
        "|---|---|---|---|---|---|---|",
    ]
    for s, v in report["scenarios"].items():
        lines.append(
            f"| {s} | {v['tasks']} | {v['expected_runbook']} | {v['runbooks']} | "
            f"{v['statuses']} | {v['spans']} | {v['traces']} |"
        )
    lines += [
        "",
        f"Runbooks: {report['runbooks']}",
        "",
        "Tasks whose runbook differs from the expected one:",
        "",
    ]
    lines += [
        f"- {m['task']}: expected {m['expected']}, got {m['got']}" for m in report["missed_runbook"]
    ] or ["- none"]
    lines += [
        "",
        "| Task | Params | Runbook | Status | Steps | Approval policy | Approvals |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in report["tasks"]:
        steps = ", ".join(f"{s['action']}:{s['status']}" for s in t["steps"]) or "-"
        approvals = ", ".join(f"{a['action']}:{a['decision']}" for a in t["approvals"]) or "-"
        lines.append(
            f"| {t['task']} | {t['params']} | {t['runbook']} | {t['status']} | {steps} | "
            f"{t['approval_policy']} | {approvals} |"
        )
    lines += ["", "Safety probes:", ""]
    lines += [
        f"- {p['check']}: {'allowed' if p['allowed'] else 'denied'} "
        f"({'ok' if p['ok'] else 'FAILED'})"
        for p in report["safety"]
    ]
    lines += ["", f"Problems: {report['problems'] or 'none'}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
