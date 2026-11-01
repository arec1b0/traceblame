# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Capture golden fixtures from real agentic-rag runs exported through kind (#12, ADR-0006).

The app (arec1b0/agentic-rag, instrumented with traceblame-py) answers real CUAD questions
with qwen2.5:7b on Ollama. It exports to the pinned otelcol-contrib, whose only pipeline is
otlp -> [file, otlphttp to the kind agent with basic auth]: no processor touches a span, so the
file is what the shim sent, and the agent gets the same requests.

1. Preflight: the same app path against a stub LLM with canary strings; nothing may leak.
2. Run A, as tenant-a (not in eval_tenants): its aegis.eval.run.id must be dropped by the
   agent and counted in traceblame_normalizer_eval_marker_rejected_total.
3. Run B, as tenant-eval, one collector session per scenario (answerable, unanswerable): the
   gateway keeps every marked trace, so ClickHouse must hold exactly the spans of the files.
4. No query or chunk text in the files or in ClickHouse. Report in evals/reports/; fixtures
   and provenance only when every check passes.

Run from WSL2 or Linux after `make kind-up kind-deploy`: `make fixtures-agentic-rag`.
"""

import argparse
import base64
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "agentic_rag"))

import capture as common  # noqa: E402 - after the path insert
import cuad  # noqa: E402

ROOT = common.ROOT
OUT = common.OUT
APP_REPOSITORY = "https://github.com/arec1b0/agentic-rag"
RUN_SCRIPT = HERE / "agentic_rag" / "run.py"
LOCK = HERE / "agentic_rag" / "requirements.lock"
VENV = ROOT / ".tools" / "agentic-rag-venv"
CACHE = ROOT / ".tools" / "cache"
REPORTS = ROOT / "evals" / "reports"
KUBE_CONTEXT = "kind-traceblame"
NAMESPACE = "observability"

INSTRUMENTATION = "openinference-instrumentation-openai"
EMBEDDING_REPO = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
EMBEDDING_FILES = ["*.json", "*.txt", "model.safetensors", "1_Pooling/*"]
SERVICE = "agentic-rag"

RUN_A_TENANT = "tenant-a"
RUN_B_TENANT = "tenant-eval"
MARKER = "aegis.eval.run.id"
REJECTED = "traceblame_normalizer_eval_marker_rejected_total"
# tail_sampling decision_wait (30 s) plus the exporters' flush, with room to spare.
SETTLE_SECONDS = 240

PACKAGES = (
    INSTRUMENTATION,
    "openinference-instrumentation",
    "openinference-semantic-conventions",
    "openai",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "traceblame-py",
    "chromadb",
    "sentence-transformers",
    "torch",
)

# The app's own switches for a capture: no query cache (each query must run), no on-disk
# embedding cache or cost log in the app checkout, its Prometheus server off the usual port.
APP_ENV = {
    "ENABLE_QUERY_CACHE": "false",
    "ENABLE_EMBEDDING_CACHE": "false",
    "ENABLE_COST_TRACKING": "false",
    "ANONYMIZED_TELEMETRY": "False",
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "TRACEBLAME_ENABLED": "1",
}
_INHERITED_PREFIXES = (*common._INHERITED_PREFIXES, "TRACEBLAME_", "HF_", "CAPTURE_")  # pyright: ignore[reportPrivateUsage]

FORWARD_CONFIG = """\
extensions:
  basicauth/agent:
    client_auth:
      username: ${{env:CAPTURE_TENANT}}
      password: ${{env:CAPTURE_TENANT_PASSWORD}}
receivers:
  otlp:
    protocols:
      http:
        endpoint: {endpoint}
exporters:
  file:
    path: {path}
    format: json
  otlp_http/agent:
    endpoint: http://127.0.0.1:{agent_port}
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
      exporters: [file, otlp_http/agent]
"""


def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kwargs)  # noqa: S603


def kubectl(*args: str) -> str:
    return run([str(ROOT / ".tools" / "bin" / "kubectl"), "--context", KUBE_CONTEXT, *args]).stdout


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args]).stdout.strip()


def require_clean(repo: Path) -> str:
    if git(repo, "status", "--porcelain"):
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


# Environment --------------------------------------------------------------------------


def ensure_venv(uv: str) -> Path:
    """The capture environment from the hash-locked requirements, rebuilt when the lock or the
    shim's metadata changes; the shim from this tree, editable, without its dependencies."""
    python = VENV / "bin" / "python"
    marker = VENV / ".lock-sha256"
    want = common.sha256(LOCK) + common.sha256(ROOT / "sdk" / "python" / "pyproject.toml")
    if not python.is_file() or not marker.is_file() or marker.read_text() != want:
        shutil.rmtree(VENV, ignore_errors=True)
        env = os.environ | {"VIRTUAL_ENV": str(VENV)}
        run([uv, "venv", "--quiet", "--python", "3.12", str(VENV)])
        run(
            [uv, "pip", "sync", "--quiet", "--require-hashes", "--torch-backend", "cpu", str(LOCK)],
            env=env,
        )
        run(
            [uv, "pip", "install", "--quiet", "--no-deps", "-e", str(ROOT / "sdk" / "python")],
            env=env,
        )
        run([uv, "pip", "check"], env=env)
        marker.write_text(want)
    return python


def installed_versions(python: Path) -> dict[str, str]:
    code = (
        "import json, sys\nfrom importlib.metadata import version\n"
        "print(json.dumps({p: version(p) for p in sys.argv[1:]}))"
    )
    return json.loads(run([str(python), "-I", "-c", code, *PACKAGES]).stdout)


def ensure_embedding_model(python: Path) -> Path:
    target = CACHE / "hf" / f"all-MiniLM-L6-v2-{EMBEDDING_REVISION}"
    if not (target / "model.safetensors").is_file():
        code = (
            "import sys\nfrom huggingface_hub import snapshot_download\n"
            "snapshot_download(sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3], "
            "allow_patterns=sys.argv[4:])"
        )
        run(
            [
                str(python),
                "-I",
                "-c",
                code,
                EMBEDDING_REPO,
                EMBEDDING_REVISION,
                str(target),
                *EMBEDDING_FILES,
            ]
        )
    return target


def app_env(
    app_dir: Path, ollama: str, embedding: Path, collector_port: int, **extra: str
) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_INHERITED_PREFIXES)}
    return (
        env
        | APP_ENV
        | {
            "CAPTURE_APP_DIR": str(app_dir),
            "OPENAI_BASE_URL": f"{ollama}/v1",
            "OPENAI_API_KEY": "ollama",
            "PRIMARY_LLM": common.CHAT_MODEL,
            "JUDGE_LLM": common.CHAT_MODEL,
            "EMBEDDING_MODEL": str(embedding),
            "METRICS_PORT": str(free_port()),
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{collector_port}",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        }
        | extra
    )


# Collector sessions --------------------------------------------------------------------


class Collector:
    """The pinned otelcol-contrib for one session: file only, or file plus the kind agent."""

    def __init__(
        self,
        workdir: Path,
        name: str,
        *,
        agent_port: int | None = None,
        tenant: tuple[str, str] | None = None,
    ) -> None:
        self.path = workdir / f"{name}.jsonl"
        config = workdir / f"{name}.otelcol.yaml"
        if agent_port is None:
            config.write_text(
                common.COLLECTOR_CONFIG.format(endpoint=common.OTLP_ENDPOINT, path=self.path)
            )
        else:
            config.write_text(
                FORWARD_CONFIG.format(
                    endpoint=common.OTLP_ENDPOINT, path=self.path, agent_port=agent_port
                )
            )
        env = dict(os.environ)
        if tenant is not None:
            env |= {"CAPTURE_TENANT": tenant[0], "CAPTURE_TENANT_PASSWORD": tenant[1]}
        self._proc = subprocess.Popen([str(common.OTELCOL), "--config", f"file:{config}"], env=env)  # noqa: S603
        common.wait_for_port(self._proc)

    def stop(self) -> dict[str, Any]:
        self._proc.send_signal(signal.SIGTERM)
        self._proc.wait(timeout=30)
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        batches = [json.loads(line) for line in lines if line]
        return {"resourceSpans": [rs for b in batches for rs in b.get("resourceSpans", [])]}


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


def run_app(python: Path, env: dict[str, str], *args: str) -> list[dict[str, Any]]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(python), "-I", str(RUN_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        # run.py prints no content; the app logs only error levels without messages.
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"capture: run.py {args[0]} failed with exit code {result.returncode}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


# Kind -----------------------------------------------------------------------------------


def tenant_password(tenant: str) -> str:
    raw = kubectl(
        "-n", "bench", "get", "secret", "tenant-credentials", "-o", f"jsonpath={{.data.{tenant}}}"
    )
    if not raw:
        raise SystemExit(f"capture: no credentials for {tenant}; run `make kind-up`")
    return base64.b64decode(raw).decode()


def check_eval_tenants() -> None:
    try:
        value = kubectl(
            "-n",
            NAMESPACE,
            "get",
            "configmap",
            "eval-tenants",
            "-o",
            "jsonpath={.data.TRACEBLAME_EVAL_TENANTS}",
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit("capture: no eval-tenants ConfigMap; run `make kind-deploy`") from exc
    tenants = yaml.safe_load(value) or []
    if RUN_B_TENANT not in tenants or RUN_A_TENANT in tenants:
        raise SystemExit(
            f"capture: eval_tenants is {tenants}; want {RUN_B_TENANT} and not {RUN_A_TENANT}"
        )


class PortForward:
    def __init__(self, target: str, remote: int) -> None:
        self.port = free_port()
        self._proc = subprocess.Popen(  # noqa: S603
            [
                str(ROOT / ".tools" / "bin" / "kubectl"),
                "--context",
                KUBE_CONTEXT,
                "-n",
                NAMESPACE,
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


def pods(label: str) -> list[str]:
    return kubectl(
        "-n", NAMESPACE, "get", "pods", "-l", label, "-o", "jsonpath={.items[*].metadata.name}"
    ).split()


_SAMPLE = re.compile(rf"^{REJECTED}\{{([^}}]*)\}} ([0-9.eE+-]+)$")


def rejected(pod: str) -> dict[tuple[str, str, str], float]:
    """The marker-rejection counter of one pod, by (tenant, attr, reason)."""
    text = kubectl("get", "--raw", f"/api/v1/namespaces/{NAMESPACE}/pods/{pod}:8888/proxy/metrics")
    out: dict[tuple[str, str, str], float] = {}
    for line in text.splitlines():
        found = _SAMPLE.match(line)
        if found:
            labels = dict(re.findall(r'(\w+)="([^"]*)"', found.group(1)))
            key = (labels.get("tenant", ""), labels.get("attr", ""), labels.get("reason", ""))
            out[key] = out.get(key, 0.0) + float(found.group(2))
    return out


def counter_total(pod_names: list[str], key: tuple[str, str, str]) -> float:
    return sum(rejected(p).get(key, 0.0) for p in pod_names)


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


# Checks -----------------------------------------------------------------------------------


def check_canary(otlp: dict[str, Any], report: dict[str, Any]) -> list[str]:
    text = json.dumps(otlp)
    problems = [f"canary {c!r} exported" for c in report["canaries"] if c in text]
    spans = [s for _, s in spans_of(otlp)]
    kinds = Counter(attr(s.get("attributes", []), "openinference.span.kind") for s in spans)
    if kinds["AGENT"] != 1 or kinds["RETRIEVER"] < 1 or kinds["TOOL"] < 2 or kinds["LLM"] < 3:
        problems.append(f"unexpected span kinds {dict(kinds)}")
    tools = [s for s in spans if attr(s.get("attributes", []), "openinference.span.kind") == "TOOL"]
    if not all(attr(s.get("attributes", []), "aegis.tool.args_hash") for s in tools):
        problems.append("a tool span has no aegis.tool.args_hash")
    llms = [s for s in spans if attr(s.get("attributes", []), "openinference.span.kind") == "LLM"]
    ok = [s for s in llms if s.get("status", {}).get("code") != 2]
    if not ok or not all(
        attr(s.get("attributes", []), "aegis.output.parse_ok") is True for s in ok
    ):
        problems.append("a successful reasoning call has no aegis.output.parse_ok=true")
    cited = [attr(s.get("attributes", []), "aegis.output.cited_doc_ids") for s in ok]
    if [report["doc_id"]] not in cited:
        problems.append(f"no call cites the canary document: {cited}")
    failed = [s for s in llms if s.get("status", {}).get("code") == 2]
    if not failed or any(
        attr(s.get("attributes", []), "aegis.output.parse_ok") is not None for s in failed
    ):
        problems.append("the failed judge call is missing or carries parse_ok")
    return problems


def resource_marker(rs: dict[str, Any]) -> Any:
    return attr(rs.get("resource", {}).get("attributes", []), MARKER)


# Main -------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--ollama-url", help="default: localhost, then the WSL2 host")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--n", type=int, default=24, help="run B: CUAD queries")
    parser.add_argument("--n-run-a", type=int, default=4, help="run A: a prefix of the same sample")
    parser.add_argument("--ratio", type=float, default=0.5, help="answerable share")
    args = parser.parse_args()

    app_dir: Path = args.app_dir.resolve()
    traceblame_commit = require_clean(ROOT)
    app_commit = require_clean(app_dir)
    if not common.OTELCOL.is_file():
        raise SystemExit("capture: run `scripts/install-tools.sh otelcol-contrib` first")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("capture: uv is not on PATH")
    uv_version = run([uv, "--version"]).stdout.split()[1]
    ollama = common.resolve_ollama(args.ollama_url)
    server_version = common.get_json(f"{ollama}/api/version")["version"]
    check_model(ollama)
    otelcol_version, otelcol_sha256 = common.tool_pin("otelcol-contrib")
    check_eval_tenants()
    passwords = {t: tenant_password(t) for t in (RUN_A_TENANT, RUN_B_TENANT)}

    python = ensure_venv(uv)
    packages = installed_versions(python)
    python_version = run(
        [str(python), "-I", "-c", "import platform; print(platform.python_version())"]
    ).stdout.strip()
    embedding = ensure_embedding_model(python)
    archive = cuad.ensure_archive(CACHE / "cuad")
    day = datetime.now(UTC).strftime("%Y%m%d")
    run_base = f"rag-cuad-{day}-s{args.seed}-{traceblame_commit[:7]}"
    sample_args = ["--archive", str(archive), "--seed", str(args.seed), "--ratio", str(args.ratio)]

    report: dict[str, Any] = {
        "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "traceblame_commit": traceblame_commit,
        "app": {"repository": APP_REPOSITORY, "commit": app_commit},
        "problems": [],
    }
    problems: list[str] = report["problems"]
    agent_pods = pods("app=otel-agent")
    gateway_pods = pods("app=otel-gateway")
    rejected_key = (RUN_A_TENANT, MARKER, "tenant_not_allowed")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # 1. Canary preflight: file only, nothing goes to kind.
        print("capture: canary preflight", file=sys.stderr)
        col = Collector(work, "canary")
        try:
            canary = run_app(
                python,
                app_env(app_dir, ollama, embedding, int(common.OTLP_ENDPOINT.split(":")[1])),
                "canary",
            )[-1]
        finally:
            canary_otlp = col.stop()
        canary_problems = check_canary(canary_otlp, canary)
        report["canary"] = {
            "steps": canary["steps"],
            "spans": len(spans_of(canary_otlp)),
            "problems": canary_problems,
        }
        if canary_problems:
            raise SystemExit(f"capture: canary preflight failed: {canary_problems}")

        forward = PortForward("svc/otel-agent", 4318)
        try:
            before_agents = counter_total(agent_pods, rejected_key)
            before_gateways = counter_total(gateway_pods, rejected_key)

            # 2. Run A: tenant-a may not mark traces.
            print("capture: run A", file=sys.stderr)
            env_a = app_env(
                app_dir,
                ollama,
                embedding,
                4318,
                TRACEBLAME_TENANT_ID=RUN_A_TENANT,
                TRACEBLAME_EVAL_RUN_ID=f"{run_base}-a",
            )
            col = Collector(
                work,
                "run_a",
                agent_port=forward.port,
                tenant=(RUN_A_TENANT, passwords[RUN_A_TENANT]),
            )
            try:
                outcomes_a = run_app(python, env_a, "run", "--n", str(args.n_run_a), *sample_args)
            finally:
                run_a = col.stop()

            # 3. Run B: tenant-eval, one collector session per scenario.
            runs_b: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
            for stratum in ("answerable", "unanswerable"):
                print(f"capture: run B, {stratum}", file=sys.stderr)
                env_b = app_env(
                    app_dir,
                    ollama,
                    embedding,
                    4318,
                    TRACEBLAME_TENANT_ID=RUN_B_TENANT,
                    TRACEBLAME_EVAL_RUN_ID=f"{run_base}-b",
                )
                col = Collector(
                    work,
                    f"run_b_{stratum}",
                    agent_port=forward.port,
                    tenant=(RUN_B_TENANT, passwords[RUN_B_TENANT]),
                )
                try:
                    outcomes = run_app(
                        python, env_b, "run", "--n", str(args.n), "--stratum", stratum, *sample_args
                    )
                finally:
                    otlp_b = col.stop()
                runs_b[stratum] = (otlp_b, outcomes)
        finally:
            forward.close()

        # 4. Let the pipeline settle, then compare with ClickHouse.
        sent_b = {
            (s["traceId"], s["spanId"]) for otlp, _ in runs_b.values() for _, s in spans_of(otlp)
        }
        deadline = time.monotonic() + SETTLE_SECONDS
        rows_b: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows_b = stored(t for t, _ in sent_b)
            if len(rows_b) >= len(sent_b):
                break
            time.sleep(10)
        time.sleep(15)  # late duplicates, and run A's sampled traces
        rows_b = stored(t for t, _ in sent_b)
        sent_a = {(s["traceId"], s["spanId"]) for _, s in spans_of(run_a)}
        rows_a = stored(t for t, _ in sent_a)
        after_agents = counter_total(agent_pods, rejected_key)
        after_gateways = counter_total(gateway_pods, rejected_key)

        got_b = {(r["trace_id"], r["span_id"]) for r in rows_b}
        all_files = [run_a, *(otlp for otlp, _ in runs_b.values())]
        duplicates = sum(len(spans_of(o)) for o in all_files) - len(sent_a) - len(sent_b)
        if duplicates:
            problems.append(f"{duplicates} spans exported twice (a retried request)")
        if got_b != sent_b or len(rows_b) != len(sent_b):
            problems.append(
                f"run B: {len(sent_b)} spans sent, {len(rows_b)} rows stored, "
                f"{len(sent_b - got_b)} missing, {len(got_b - sent_b)} unexpected"
            )
        if any(r["tenant_id"] != RUN_B_TENANT for r in rows_b):
            problems.append("run B: a row with another tenant")
        if any(r["resource_attrs"].get(MARKER) != f"{run_base}-b" for r in rows_b):
            problems.append("run B: a row without the eval run marker")
        if not all(
            resource_marker(rs) == f"{run_base}-b"
            for otlp, _ in runs_b.values()
            for rs in otlp["resourceSpans"]
        ):
            problems.append("run B: a resource block without the eval run marker in the file")

        got_a = {(r["trace_id"], r["span_id"]) for r in rows_a}
        if not got_a <= sent_a:
            problems.append("run A: stored spans that were not sent")
        traces_a = {t for t, _ in got_a}
        incomplete = [
            t
            for t in traces_a
            if {s for tt, s in sent_a if tt == t} != {s for tt, s in got_a if tt == t}
        ]
        if incomplete:
            problems.append(f"run A: {len(incomplete)} stored traces incomplete")
        if any(MARKER in r["resource_attrs"] for r in rows_a):
            problems.append("run A: a stored row kept the marker")
        if any(r["tenant_id"] != RUN_A_TENANT for r in rows_a):
            problems.append("run A: a row with another tenant")
        blocks_a = sum(1 for rs in run_a["resourceSpans"] if resource_marker(rs) == f"{run_base}-a")
        delta_agents = after_agents - before_agents
        delta_gateways = after_gateways - before_gateways
        if delta_agents != blocks_a:
            problems.append(
                f"run A: agent counter rose by {delta_agents}, "
                f"{blocks_a} marked resource blocks sent"
            )
        if delta_gateways != 0:
            problems.append(f"run A: gateway counter rose by {delta_gateways}")

        # 5. No query or chunk text anywhere.
        qids = sorted(
            {o["qid"] for o in outcomes_a} | {o["qid"] for _, outs in runs_b.values() for o in outs}
        )
        files = []
        for name, payload in (
            ("run_a", run_a),
            *((f"run_b_{k}", v[0]) for k, v in runs_b.items()),
            ("stored", rows_a + rows_b),
        ):
            path = work / f"{name}.check.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            files.append(str(path))
        check = run_app(
            python,
            app_env(app_dir, ollama, embedding, 4318),
            "check-content",
            "--archive",
            str(archive),
            "--qids",
            *qids,
            "--files",
            *files,
        )
        report["content_check"] = check[-1]
        if check[-1]["leaking_qids"]:
            problems.append(f"content found for {len(check[-1]['leaking_qids'])} queries")

    report["run_a"] = {
        "eval_run_id": f"{run_base}-a",
        "tenant": RUN_A_TENANT,
        "queries": len(outcomes_a),
        "outcomes": dict(Counter(o["outcome"] for o in outcomes_a)),
        "spans_sent": len(sent_a),
        "spans_stored": len(rows_a),
        "traces_stored": len(traces_a),
        "marked_resource_blocks": blocks_a,
        "agent_rejected_delta": delta_agents,
        "gateway_rejected_delta": delta_gateways,
    }
    report["run_b"] = {
        "eval_run_id": f"{run_base}-b",
        "tenant": RUN_B_TENANT,
        "scenarios": {
            k: {
                "queries": len(outs),
                "outcomes": dict(Counter(o["outcome"] for o in outs)),
                "spans": len(spans_of(otlp)),
                "traces": len({s["traceId"] for _, s in spans_of(otlp)}),
            }
            for k, (otlp, outs) in runs_b.items()
        },
        "spans_sent": len(sent_b),
        "spans_stored": len(rows_b),
    }
    stem = f"sdk-py-kind-{datetime.now(UTC).strftime('%Y-%m-%d')}"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (REPORTS / f"{stem}.md").write_text(markdown(report), encoding="utf-8")
    print(f"capture: wrote evals/reports/{stem}.md", file=sys.stderr)
    if problems:
        raise SystemExit("capture: checks failed, no fixtures written:\n  " + "\n  ".join(problems))

    # 6. Fixtures and provenance.
    target = OUT / "openinference" / f"{INSTRUMENTATION}-{packages[INSTRUMENTATION]}"
    target.mkdir(parents=True, exist_ok=True)
    for stratum, (otlp, outs) in runs_b.items():
        scenario = f"agentic_rag_cuad_{stratum}"
        fixture = target / f"{scenario}.otlp.json"
        fixture.write_text(json.dumps(otlp, indent=2) + "\n", encoding="utf-8")
        provenance = {
            "sdk": "openinference",
            "scenario": scenario,
            "captured_at": report["captured_at"],
            "capture_script": "tools/fixtures/capture_agentic_rag.py",
            "capture_script_sha256": common.sha256(Path(__file__)),
            "python": python_version,
            "uv": uv_version,
            "packages": packages,
            "env": dict(APP_ENV)
            | {"TRACEBLAME_TENANT_ID": RUN_B_TENANT, "TRACEBLAME_EVAL_RUN_ID": f"{run_base}-b"},
            "content_capture": False,
            "content_check": "passed",
            "model": {
                "name": common.CHAT_MODEL,
                "digest": common.CHAT_MODEL_DIGEST,
                "server": "ollama",
                "server_version": server_version,
            },
            "endpoint": f"{ollama}/v1",
            "collector": f"otelcol-contrib {otelcol_version} (sha256 {otelcol_sha256})",
            "fixture_sha256": common.sha256(fixture),
            "app": {"repository": APP_REPOSITORY, "commit": app_commit},
            "shim": {
                "package": "traceblame-py",
                "version": packages["traceblame-py"],
                "commit": traceblame_commit,
            },
            "dataset": {
                "name": "CUAD v1, official test split",
                "source": cuad.CUAD_URL,
                "archive_sha256": cuad.CUAD_SHA256,
                "license": cuad.LICENSE,
                "seed": args.seed,
                "n": args.n,
                "ratio": args.ratio,
                "stratum": stratum,
                "qids": [o["qid"] for o in outs],
            },
            "embedding_model": {"name": EMBEDDING_REPO, "revision": EMBEDDING_REVISION},
            "run": {
                "script": "tools/fixtures/agentic_rag/run.py",
                "script_sha256": common.sha256(RUN_SCRIPT),
                "outcomes": dict(Counter(o["outcome"] for o in outs)),
                "report": f"evals/reports/{stem}.md",
            },
            "notes": [
                "Real CUAD questions, verbatim, each over the contract it is about; the app's "
                "RAGAgent with its retriever, summarizer and calculator tools, and its LLM judge.",
                "Exported through traceblame-py: content stays in-process (ADR-0006). The file is "
                "what the pinned collector received; the kind agent got the same requests.",
                common._OPENAI_VIA_OLLAMA,  # pyright: ignore[reportPrivateUsage]
            ],
        }
        fixture.with_name(f"{scenario}.provenance.yaml").write_text(
            yaml.safe_dump(provenance, sort_keys=False, width=100), encoding="utf-8"
        )
        print(f"capture: wrote {fixture.relative_to(ROOT)}", file=sys.stderr)
    return 0


def markdown(report: dict[str, Any]) -> str:
    a, b = report["run_a"], report["run_b"]
    canary = report["canary"]
    canary_result = canary["problems"] or "no problems"
    counter = f'{REJECTED}{{tenant="{a["tenant"]}",attr="{MARKER}",reason="tenant_not_allowed"}}'
    checked = report["content_check"]["checked_qids"]
    content = "FAILED" if report["content_check"]["leaking_qids"] else "passed"
    lines = [
        f"# traceblame-py through kind: agentic-rag on CUAD ({report['captured_at'][:10]})",
        "",
        "`make fixtures-agentic-rag` (#12): the agentic-rag app, instrumented with traceblame-py,",
        "answers real CUAD questions with qwen2.5:7b and exports through the pinned collector to",
        "the kind agent and gateway (ADR-0005, ADR-0006). Machine-readable: the `.json` next to",
        "this file.",
        "",
        f"- traceblame `{report['traceblame_commit'][:12]}`, app `{report['app']['commit'][:12]}`",
        f"- Canary preflight: {canary['spans']} spans, steps {canary['steps']}, {canary_result}",
        "",
        "## Run B: eval tenant, every marked trace kept",
        "",
        f"`{b['eval_run_id']}` as `{b['tenant']}`.",
        "",
        "| Scenario | Queries | Traces | Spans | Outcomes |",
        "|---|---|---|---|---|",
        *(
            f"| {k} | {v['queries']} | {v['traces']} | {v['spans']} | {v['outcomes']} |"
            for k, v in b["scenarios"].items()
        ),
        "",
        f"Spans sent {b['spans_sent']}, rows in ClickHouse {b['spans_stored']} "
        "(the same span ids are required).",
        "",
        "## Run A: tenant not in eval_tenants",
        "",
        f"`{a['eval_run_id']}` as `{a['tenant']}`: {a['queries']} queries, "
        f"outcomes {a['outcomes']}.",
        "",
        f"- Marked resource blocks sent: {a['marked_resource_blocks']}; "
        f"`{counter}` rose by {a['agent_rejected_delta']:g} on the agents and "
        f"{a['gateway_rejected_delta']:g} on the gateways.",
        f"- Spans sent {a['spans_sent']}; stored {a['spans_stored']} in {a['traces_stored']} "
        "complete traces (10% sampling once the marker is dropped), none with the marker.",
        "",
        "## Content",
        "",
        f"No query text and no 48-character window of any chunk of the {checked} queried "
        f"contracts in the exported files or the stored rows: {content}.",
        "",
        "## Result",
        "",
        "All checks passed."
        if not report["problems"]
        else "Failed:\n\n" + "\n".join(f"- {p}" for p in report["problems"]),
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
