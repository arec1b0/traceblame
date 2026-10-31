# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Capture golden span fixtures from real LLM calls (issues #3, #8; ADR-0001).

For each capture: start the pinned otelcol-contrib (OTLP/HTTP in, file exporter out), run
the SDK's capture script against Ollama's OpenAI-compatible API, stop the collector, and
write <scenario>.otlp.json plus <scenario>.provenance.yaml under collector/testdata/fixtures.
Every SDK mode captures every request in REQUESTS. Content capture is off in every SDK.

Run from WSL2 or Linux: `make fixtures` (see collector/testdata/fixtures/README.md).
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = ROOT / "collector" / "testdata" / "fixtures"
OTELCOL = ROOT / ".tools" / "bin" / "otelcol-contrib"
OTLP_ENDPOINT = "127.0.0.1:4318"

# Every capture makes one request through the OpenAI client against Ollama. Prompts are
# harmless public facts; with content capture off they are never exported. Each request
# pins the Ollama model digest it must run on: a capture fails on any other model build.
CHAT_MODEL = "qwen2.5:7b"
CHAT_MODEL_DIGEST = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_MODEL_DIGEST = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
PROMPT = "Name the capital of France in one word."
TOOL_PROMPT = "What is the weather in Paris right now?"
EMBEDDING_INPUT = "The capital of France is Paris."
SEED = 7
MAX_TOKENS = 16
TOOL_MAX_TOKENS = 64

# Drop inherited settings that would change what an instrumentation emits.
_INHERITED_PREFIXES = ("OTEL_", "TRACELOOP_", "OPENINFERENCE_", "OPENAI_")

_OPENAI_VIA_OLLAMA = (
    "gen_ai.provider.name / server.address describe the OpenAI client talking to Ollama's "
    "OpenAI-compatible API, not the OpenAI service."
)


@dataclass(frozen=True)
class Request:
    """One request kind of tools/fixtures/_capture_common.py and what it must produce."""

    name: str
    model: str
    digest: str
    prompt: str
    max_tokens: int
    note: str
    expected: dict[str, Any]


REQUESTS = {
    r.name: r
    for r in (
        Request(
            name="chat_basic",
            model=CHAT_MODEL,
            digest=CHAT_MODEL_DIGEST,
            prompt=PROMPT,
            max_tokens=MAX_TOKENS,
            note=f"chat.completions.create, temperature=0, seed={SEED}, max_tokens={MAX_TOKENS}",
            expected={"finish_reason": "stop"},
        ),
        Request(
            name="chat_tool_call",
            model=CHAT_MODEL,
            digest=CHAT_MODEL_DIGEST,
            prompt=TOOL_PROMPT,
            max_tokens=TOOL_MAX_TOKENS,
            note=(
                "chat.completions.create with one function tool (get_weather), temperature=0, "
                f"seed={SEED}, max_tokens={TOOL_MAX_TOKENS}; the model answers with a tool call"
            ),
            expected={"finish_reason": "tool_calls", "tool_calls": ["get_weather"]},
        ),
        Request(
            name="chat_error",
            model=CHAT_MODEL,
            digest=CHAT_MODEL_DIGEST,
            prompt="",
            max_tokens=MAX_TOKENS,
            note="chat.completions.create with messages=[]: HTTP 400 invalid_request_error",
            expected={"error": "BadRequestError", "status_code": 400},
        ),
        Request(
            name="embeddings_basic",
            model=EMBEDDING_MODEL,
            digest=EMBEDDING_MODEL_DIGEST,
            prompt=EMBEDDING_INPUT,
            max_tokens=0,
            note="embeddings.create with one input",
            expected={"embeddings": 1, "dimensions": 768},
        ),
    )
}


@dataclass(frozen=True)
class Capture:
    sdk: str
    scenario: str
    script: str
    instrumentation: str
    env: dict[str, str]
    request: Request
    notes: tuple[str, ...] = ()


# Content-off settings of each SDK (what the shims will set).
_OPENINFERENCE_HIDE = {
    f"OPENINFERENCE_HIDE_{name}": "true"
    for name in (
        "INPUTS",
        "OUTPUTS",
        "INPUT_MESSAGES",
        "OUTPUT_MESSAGES",
        "INPUT_TEXT",
        "OUTPUT_TEXT",
        "EMBEDDINGS_TEXT",
        "EMBEDDINGS_VECTORS",
        "PROMPTS",
    )
}
_OTEL_GENAI_NOTES = (
    "openai 2.x: instrumentation 2.4b0 cannot load next to openai 3.x (httpx -> httpx2).",
    "opentelemetry-util-genai 0.4b0: 2.4b0 breaks on 1.3b0 (removed module).",
)

# (sdk, script, instrumentation, env, scenario suffix, notes)
_SDK_MODES: tuple[tuple[str, str, str, dict[str, str], str, tuple[str, ...]], ...] = (
    (
        "openinference",
        "capture_openinference.py",
        "openinference-instrumentation-openai",
        _OPENINFERENCE_HIDE,
        "",
        (),
    ),
    (
        "openllmetry",
        "capture_openllmetry.py",
        "opentelemetry-instrumentation-openai",
        {"TRACELOOP_TRACE_CONTENT": "false"},
        "",
        (),
    ),
    (
        "otel-genai",
        "capture_otel_genai.py",
        "opentelemetry-instrumentation-openai-v2",
        {},
        "",
        ("Default semconv mode of the instrumentation.", *_OTEL_GENAI_NOTES),
    ),
    (
        "otel-genai",
        "capture_otel_genai.py",
        "opentelemetry-instrumentation-openai-v2",
        {"OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental"},
        "_latest_experimental",
        ("Latest experimental GenAI semconv mode of the instrumentation.", *_OTEL_GENAI_NOTES),
    ),
)

CAPTURES = tuple(
    Capture(
        sdk=sdk,
        scenario=f"{request.name}{suffix}",
        script=script,
        instrumentation=instrumentation,
        env=env,
        request=request,
        notes=notes,
    )
    for sdk, script, instrumentation, env, suffix, notes in _SDK_MODES
    for request in REQUESTS.values()
)

COLLECTOR_CONFIG = """\
receivers:
  otlp:
    protocols:
      http:
        endpoint: {endpoint}
exporters:
  file:
    path: {path}
    format: json
service:
  telemetry:
    metrics:
      level: none
    logs:
      level: warn
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [file]
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get_json(url: str) -> Any:
    if not url.startswith(("http://", "https://")):
        raise SystemExit(f"capture: unsupported URL {url}")
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - scheme checked above
        return json.load(resp)


def wsl_host_url() -> list[str]:
    """Under WSL2 NAT networking the Windows host is the default gateway."""
    try:
        if "microsoft" not in Path("/proc/version").read_text(encoding="utf-8").lower():
            return []
        route = subprocess.run(
            ["ip", "route", "show", "default"],  # noqa: S607 - standard iproute2 tool
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return []
    return [f"http://{route[route.index('via') + 1]}:11434"] if "via" in route else []


def resolve_ollama(url: str | None) -> str:
    candidates = [url] if url else ["http://localhost:11434", *wsl_host_url()]
    for candidate in candidates:
        try:
            get_json(f"{candidate}/api/version")
            return candidate
        except OSError:
            continue
    raise SystemExit(f"capture: Ollama is not reachable at {candidates}; pass --ollama-url")


def tool_pin(name: str) -> tuple[str, str]:
    versions = dict(
        line.split("=", 1)
        for line in (ROOT / "tools" / "versions.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    version = versions["OTELCOL_CONTRIB_VERSION"]
    archive = f"{name}_{version}_linux_amd64.tar.gz"
    for line in (ROOT / "tools" / "checksums.txt").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split()
        if filename == archive:
            return version, digest
    raise SystemExit(f"capture: no checksum for {archive}")


def check_models(ollama: str, requests: Iterable[Request]) -> None:
    """Fail unless every model is pulled on the server with exactly the pinned digest."""
    pulled = {
        m["name"].removesuffix(":latest"): m["digest"]
        for m in get_json(f"{ollama}/api/tags")["models"]
    }
    for request in sorted(requests, key=lambda r: r.name):
        digest = pulled.get(request.model.removesuffix(":latest"))
        if digest is None:
            raise SystemExit(f"capture: model {request.model} is not pulled on {ollama}")
        if digest != request.digest:
            raise SystemExit(
                f"capture: model {request.model} on {ollama} has digest {digest}, "
                f"pinned {request.digest}"
            )


def wait_for_port(proc: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    host, port = OTLP_ENDPOINT.split(":")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"capture: otelcol-contrib exited with {proc.returncode}")
        with socket.socket() as sock:
            if sock.connect_ex((host, int(port))) == 0:
                return
        time.sleep(0.2)
    raise SystemExit("capture: otelcol-contrib did not open the OTLP port")


def run_capture(capture: Capture, uv: str, ollama: str, workdir: Path) -> dict[str, Any]:
    traces = workdir / "traces.jsonl"
    config = workdir / "otelcol.yaml"
    config.write_text(
        COLLECTOR_CONFIG.format(endpoint=OTLP_ENDPOINT, path=traces), encoding="utf-8"
    )
    collector = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(OTELCOL), "--config", f"file:{config}"]
    )
    try:
        wait_for_port(collector)
        env = {k: v for k, v in os.environ.items() if not k.startswith(_INHERITED_PREFIXES)}
        env |= {
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{OTLP_ENDPOINT}",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "CAPTURE_OPENAI_BASE_URL": f"{ollama}/v1",
            "CAPTURE_REQUEST": capture.request.name,
            "CAPTURE_MODEL": capture.request.model,
            "CAPTURE_PROMPT": capture.request.prompt,
            "CAPTURE_SEED": str(SEED),
            "CAPTURE_MAX_TOKENS": str(capture.request.max_tokens),
            **capture.env,
        }
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [uv, "run", "--quiet", "--locked", "--script", str(HERE / capture.script)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
    finally:
        collector.send_signal(signal.SIGTERM)
        collector.wait(timeout=30)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(f"capture: {capture.script} failed with exit code {result.returncode}")
    report: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    if report["outcome"] != capture.request.expected:
        raise SystemExit(
            f"capture: {capture.sdk}/{capture.scenario}: outcome {report['outcome']} "
            f"is not {capture.request.expected}"
        )
    batches = [json.loads(line) for line in traces.read_text("utf-8").splitlines() if line]
    report["otlp"] = {"resourceSpans": [rs for b in batches for rs in b.get("resourceSpans", [])]}
    if not any(
        ss.get("spans") for rs in report["otlp"]["resourceSpans"] for ss in rs.get("scopeSpans", [])
    ):
        raise SystemExit(f"capture: {capture.sdk}/{capture.scenario} exported no spans")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ollama-url", help="default: localhost, then the WSL2 host")
    parser.add_argument(
        "--only", help="capture only this sdk/scenario, e.g. openllmetry/chat_basic"
    )
    args = parser.parse_args()

    if not OTELCOL.is_file():
        raise SystemExit("capture: run `scripts/install-tools.sh otelcol-contrib` first")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("capture: uv is not on PATH")
    uv_version = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [uv, "--version"], check=True, capture_output=True, text=True
    ).stdout.split()[1]
    ollama = resolve_ollama(args.ollama_url)
    server_version = get_json(f"{ollama}/api/version")["version"]
    check_models(ollama, {c.request.name: c.request for c in CAPTURES}.values())
    otelcol_version, otelcol_sha256 = tool_pin("otelcol-contrib")

    for capture in CAPTURES:
        if args.only and args.only != f"{capture.sdk}/{capture.scenario}":
            continue
        print(f"capture: {capture.sdk}/{capture.scenario}", file=sys.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            report = run_capture(capture, uv, ollama, Path(tmp))
        packages: dict[str, str] = report["packages"]
        target = (
            OUT / capture.sdk / f"{capture.instrumentation}-{packages[capture.instrumentation]}"
        )
        target.mkdir(parents=True, exist_ok=True)
        fixture = target / f"{capture.scenario}.otlp.json"
        fixture.write_text(json.dumps(report["otlp"], indent=2) + "\n", encoding="utf-8")
        provenance = {
            "sdk": capture.sdk,
            "scenario": capture.scenario,
            "captured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "capture_script": f"tools/fixtures/{capture.script}",
            "capture_script_sha256": sha256(HERE / capture.script),
            "capture_common_sha256": sha256(HERE / "_capture_common.py"),
            "python": report["python"],
            "uv": uv_version,
            "packages": packages,
            "env": capture.env,
            "model": {
                "name": capture.request.model,
                "digest": capture.request.digest,
                "server": "ollama",
                "server_version": server_version,
            },
            "endpoint": f"{ollama}/v1",
            "collector": f"otelcol-contrib {otelcol_version} (sha256 {otelcol_sha256})",
            "fixture_sha256": sha256(fixture),
            "notes": [capture.request.note, _OPENAI_VIA_OLLAMA, *capture.notes],
        }
        fixture.with_name(f"{capture.scenario}.provenance.yaml").write_text(
            yaml.safe_dump(provenance, sort_keys=False, width=100), encoding="utf-8"
        )
        print(f"capture: wrote {fixture.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
