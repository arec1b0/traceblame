"""Faults injected into real runbook-agent runs in the kind sandbox (#14): malformed_output (model)
and prompt_variant (prompt), at the LLM seam, through the eval image's fetch preload.

The eval image is built FROM the capture image (tools/fixtures/capture_runbook_agent.py
build_image) and adds only packages/faults/preload/faults-preload.mjs and NODE_OPTIONS; the
app's own image and every non-eval manifest carry neither (tests/scripts/
test_faults_preload_guard.py, and the image checks below). The preload is a no-op unless
TRACEBLAME_FAULTS=1 and the tenant is an eval tenant.

Phases (only run, record and check touch kind; run them under
flock /tmp/traceblame-kind.lock -c '...'):

  build    (no kind)  capture image and eval image, image guard checks, the seeded plan
  run      (kind)     the eval image into kind, the sandbox, one task at a time as tenant-eval;
                      before each task the driver arms /tmp/faults/armed.json in the agent pod,
                      after it reads the preload's ledger lines; the injector's ledger
  record   (kind)     the ledger into eval_traces and injections
  check    (kind)     as for agentic-rag (faultslib.check_run), content: alert text and prompts
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fixtures"))
sys.path.insert(0, str(HERE.parent / "fixtures" / "runbook_agent"))
sys.path.insert(0, str(HERE))

import capture as common  # noqa: E402
import capture_runbook_agent as rb  # noqa: E402
import faultslib  # noqa: E402
import scenarios  # noqa: E402
from kindlib import check_model, kubectl, require_clean, run, spans_of, tenant_password  # noqa: E402

from traceblame_faults import bundle, kinds, ledger, plan, record, scenario  # noqa: E402

ROOT = common.ROOT
APP = "runbook-agent"
PRELOAD = ROOT / "packages" / "faults" / "preload" / "faults-preload.mjs"
PRELOAD_DIR = "/opt/traceblame-faults"
PRELOAD_PATH = f"{PRELOAD_DIR}/faults-preload.mjs"
FAULTS_DIR = "/tmp/faults"  # noqa: S108 - the agent pod's own emptyDir
TRIAGE_PROMPT = "src/agent/prompts/triage.prompt.ts"


def log(msg: str) -> None:
    print(f"faults[{APP}]: {msg}", file=sys.stderr, flush=True)


def eval_dockerfile(base_tag: str) -> str:
    """The eval image: the capture image plus the preload, nothing else."""
    return (
        f"FROM {base_tag}\n"
        f"COPY faults-preload.mjs {PRELOAD_PATH}\n"
        f"ENV NODE_OPTIONS=--import={PRELOAD_PATH}\n"
    )


def triage_prompt(app_dir: Path, commit: str) -> str:
    """TRIAGE_SYSTEM_PROMPT at the app commit: a template literal without interpolation."""
    source = run(["git", "-C", str(app_dir), "show", f"{commit}:{TRIAGE_PROMPT}"]).stdout
    found = re.search(r"TRIAGE_SYSTEM_PROMPT = `(.*?)`;", source, re.DOTALL)
    if found is None or "${" in found.group(1) or "\\" in found.group(1):
        raise SystemExit(f"faults: no plain TRIAGE_SYSTEM_PROMPT in {TRIAGE_PROMPT}")
    return found.group(1)


def _image_env(tag: str) -> list[str]:
    out = run(["docker", "image", "inspect", "--format", "{{json .Config.Env}}", tag]).stdout
    return cast("list[str]", json.loads(out) or [])


def _has_file(tag: str, path: str) -> bool:
    probe = subprocess.run(  # noqa: S603 - fixed argv
        ["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", f"test -e {path}"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def _fetch_name(tag: str, env: dict[str, str]) -> str:
    argv = ["docker", "run", "--rm"]
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    argv += ["--entrypoint", "node", tag, "-e", "process.stdout.write(globalThis.fetch.name)"]
    return run(argv).stdout.strip()


def image_guard(base: str, eval_tag: str) -> dict[str, Any]:
    """The capture image has no preload and no NODE_OPTIONS; the eval image has both, and its
    preload leaves fetch alone without the switch and an eval tenant."""
    on = {
        "TRACEBLAME_FAULTS": "1",
        "TRACEBLAME_TENANT_ID": faultslib.EVAL_TENANT,
        "TRACEBLAME_EVAL_TENANTS": f"[{faultslib.EVAL_TENANT}]",
    }
    out = {
        "base_node_options": any(e.startswith("NODE_OPTIONS=") for e in _image_env(base)),
        "base_has_preload": _has_file(base, PRELOAD_DIR),
        "eval_node_options": f"NODE_OPTIONS=--import={PRELOAD_PATH}" in _image_env(eval_tag),
        "eval_has_preload": _has_file(eval_tag, PRELOAD_PATH),
        "fetch_without_switch": _fetch_name(eval_tag, {}),
        "fetch_other_tenant": _fetch_name(eval_tag, on | {"TRACEBLAME_TENANT_ID": "tenant-a"}),
        "fetch_eval_tenant": _fetch_name(eval_tag, on),
    }
    ok = (
        not out["base_node_options"]
        and not out["base_has_preload"]
        and out["eval_node_options"]
        and out["eval_has_preload"]
        and out["fetch_without_switch"] == "fetch"
        and out["fetch_other_tenant"] == "fetch"
        and out["fetch_eval_tenant"] == "handle"
    )
    if not ok:
        raise SystemExit(f"faults: image guard failed: {out}")
    return out


def _python_image() -> str:
    return rb._pins()["PYTHON_IMAGE"]  # pyright: ignore[reportPrivateUsage]


def _tasks(seed: int, cfg: dict[str, Any]) -> list[Any]:
    wanted = set(cfg["scenarios"])
    return [t for t in scenarios.plan(seed, int(cfg["per_scenario"])) if t.scenario in wanted]


def _ref(task: Any, seed: int) -> str:
    return f"{task.name}@s{seed}"


# build ------------------------------------------------------------------------------------


def build(args: argparse.Namespace) -> int:
    cfg = faultslib.config()
    rcfg = cfg["runbook_agent"]
    seed = int(cfg["seed"])
    app_dir: Path = args.app_dir.resolve()
    traceblame_commit = require_clean(ROOT)
    app_commit = require_clean(app_dir)
    day = datetime.now(UTC).strftime("%Y%m%d")
    run_id = args.run_id or f"faults-{day}-s{seed}-{traceblame_commit[:7]}"
    d = faultslib.run_dir(run_id, APP)
    d.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        log("capture image")
        built = rb.build_image(app_dir, app_commit, traceblame_commit, work, load=False)
        ctx = work / "eval"
        ctx.mkdir()
        (ctx / "faults-preload.mjs").write_bytes(PRELOAD.read_bytes())
        (ctx / "Dockerfile").write_text(eval_dockerfile(built["image"]), encoding="utf-8")
        eval_tag = f"{built['image']}-faults"
        log("eval image")
        run(["docker", "build", "--quiet", "--tag", eval_tag, str(ctx)])
    guard = image_guard(built["image"], eval_tag)
    eval_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", eval_tag]).stdout.strip()
    baseline_text = triage_prompt(app_dir, app_commit)
    baseline = bundle.digest(APP, {"system": baseline_text})
    tasks = _tasks(seed, rcfg)
    queries = [
        plan.Query(
            ref=_ref(t, seed), app=APP, source="runbook", stratum=t.scenario, golden_status="none"
        )
        for t in tasks
    ]
    entries = plan.plan(
        seed=seed,
        eval_run_id=run_id,
        app=APP,
        queries=queries,
        counts={
            "malformed_output": int(rcfg["malformed_output"]),
            "prompt_variant": int(rcfg["prompt_variant"]),
        },
        clean=int(rcfg["clean"]),
        scenarios=scenario.load_all(),
        exclusive=True,
    )
    text = plan.dumps(entries)
    (d / "plan.jsonl").write_text(text, encoding="utf-8")
    (d / "queries.jsonl").write_text(plan.dump_queries(queries), encoding="utf-8")
    state: dict[str, Any] = {
        "run_id": run_id,
        "app": APP,
        "app_dir": str(app_dir),
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "traceblame_commit": traceblame_commit,
        "app_commit": app_commit,
        "seed": seed,
        "config": rcfg,
        "build": built | {"eval_image": eval_tag, "eval_image_id": eval_id},
        "image_guard": guard,
        "preload_sha256": common.sha256(PRELOAD),
        "baseline_digest": baseline,
        "plan": {
            "entries": len(entries),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "by_kind": dict(Counter(e.kind or "clean" for e in entries)),
        },
    }
    faultslib.save_state(d, state)
    print(run_id)
    return 0


# run --------------------------------------------------------------------------------------


def _arm(task: Any, entry: plan.Entry, variants: dict[str, str]) -> None:
    armed: dict[str, Any] = {
        "task": task.name,
        "query_ref": entry.query_ref,
        "seed": entry.seed,
        "kind": entry.kind,
        "injection_id": entry.injection_id,
        "params": dict(entry.params),
    }
    if entry.kind == "prompt_variant":
        armed["system_text"] = variants[str(entry.params["variant"])]
    kubectl(
        "-n", rb.SANDBOX, "exec", "-i", "deploy/runbook-agent", "--", "sh", "-c",
        f"mkdir -p {FAULTS_DIR} && cat > {FAULTS_DIR}/armed.json.tmp"
        f" && mv {FAULTS_DIR}/armed.json.tmp {FAULTS_DIR}/armed.json",
        input=json.dumps(armed),
    )  # fmt: skip


def _disarm() -> None:
    kubectl(
        "-n",
        rb.SANDBOX,
        "exec",
        "deploy/runbook-agent",
        "--",
        "rm",
        "-f",
        f"{FAULTS_DIR}/armed.json",
    )


def _preload_lines(task: Any) -> list[dict[str, Any]]:
    text = kubectl(
        "-n", rb.SANDBOX, "exec", "deploy/runbook-agent", "--", "sh", "-c",
        f"cat {FAULTS_DIR}/ledger.jsonl 2>/dev/null || true",
    )  # fmt: skip
    lines = [cast("dict[str, Any]", json.loads(x)) for x in text.splitlines() if x.strip()]
    return [x for x in lines if x.get("task") == task.name]


def _record(
    entry: plan.Entry,
    task: Any,
    lines: list[dict[str, Any]],
    run_id: str,
    digest: str,
    baseline: str,
) -> ledger.Record | None:
    traces = {x["trace_id"] for x in lines if x.get("trace_id")}
    if len(traces) != 1:
        return None
    applied = [x for x in lines if x.get("applied") and x.get("marker") == "set"]
    common_fields: dict[str, Any] = {
        "trace_id": traces.pop(),
        "eval_run_id": run_id,
        "service": APP,
        "source": "runbook",
        "query_ref": entry.query_ref,
        "stratum": task.scenario,
        "golden_doc_ids": (),
        "golden_status": "none",
        "planned_fault_kind": entry.kind,
        "seed": entry.seed,
        "bundle_digest": digest,
    }
    if not applied:
        return ledger.Record(
            **common_fields, injection_id="", fault_kind="", target_component="", params={}
        )
    extra: dict[str, Any] = {"baseline_digest": baseline}
    if entry.kind == "prompt_variant":
        extra["bundle_digest"] = digest
    return ledger.Record(
        **common_fields,
        injection_id=entry.injection_id,
        fault_kind=entry.kind,
        target_component=kinds.KINDS[entry.kind].component,
        params=dict(entry.params) | extra,
        injected_at=str(applied[0]["at"]),
    )


def run_phase(args: argparse.Namespace) -> int:
    cfg = faultslib.config()
    rcfg = cfg["runbook_agent"]
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    seed = int(state["seed"])
    ollama = common.resolve_ollama(args.ollama_url)
    check_model(ollama)
    server_version = common.get_json(f"{ollama}/api/version")["version"]
    tenants = faultslib.require_eval_tenant()
    password = tenant_password(faultslib.EVAL_TENANT)
    entries = plan.loads((d / "plan.jsonl").read_text(encoding="utf-8"))
    sc = scenario.load_all()[("prompt_variant", APP)]
    variants = {n: v.templates["system"] for n, v in sc.variants.items()}
    by_ref = {_ref(t, seed): t for t in _tasks(seed, rcfg)}
    baseline = str(state["baseline_digest"])
    blocks: dict[str, list[plan.Entry]] = {baseline: []}
    for e in entries:
        if e.kind == "prompt_variant":
            digest = bundle.digest(APP, {"system": variants[str(e.params["variant"])]})
            blocks.setdefault(digest, []).append(e)
        else:
            blocks[baseline].append(e)
    run(
        [
            str(ROOT / ".tools" / "bin" / "kind"),
            "load",
            "docker-image",
            "--name",
            "traceblame",
            state["build"]["eval_image"],
        ]
    )
    kubectl(
        "-n",
        rb.SANDBOX,
        "delete",
        "deployment,service,pod",
        "-l",
        f"{scenarios.LABEL}=workload",
        "--wait=true",
    )
    # A clean slate, as the capture's: Alertmanager without earlier groups.
    kubectl("-n", rb.MONITORING, "rollout", "restart", "deployment/alertmanager")
    kubectl("-n", rb.MONITORING, "rollout", "status", "deployment/alertmanager", "--timeout=300s")
    used = [by_ref[e.query_ref] for e in entries]
    names = scenarios.check_names(
        [o for t in used for o in scenarios.manifests(t, _python_image())]
    )
    if names:
        raise SystemExit(f"faults: workload names: {names}")
    rb.apply(
        [o for t in used if t.scenario == "oom" for o in scenarios.manifests(t, _python_image())]
    )
    seq = faultslib.stream_last_seq()
    base_env = {
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "ollama",
        "EXECUTION_MODE": "live",
        "APPROVAL_TIMEOUT_MS": str(rcfg["approval_timeout_ms"]),
        "LOG_LEVEL": "warn",
        "TRACEBLAME_ENABLED": "1",
        "TRACEBLAME_TENANT_ID": faultslib.EVAL_TENANT,
        "TRACEBLAME_EVAL_RUN_ID": state["run_id"],
        "TRACEBLAME_FAULTS": "1",
        "TRACEBLAME_EVAL_TENANTS": tenants,
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://tee.{rb.SANDBOX}.svc:4318",
        "LLM_MODEL": common.CHAT_MODEL,
        "LLM_BASE_URL": rb.OLLAMA_IN_CLUSTER,
        "LLM_STRUCTURED_OUTPUTS": "true",
    }
    build = state["build"] | {"image": state["build"]["eval_image"]}
    (d / "ledger.jsonl").unlink(missing_ok=True)
    outcomes: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    needles: set[str] = set()
    problems: list[str] = []
    safety: list[dict[str, Any]] = []
    started = time.monotonic()
    for digest, block in blocks.items():
        if not block:
            continue
        rb.deploy_agent(build, base_env | {"TRACEBLAME_BUNDLE_DIGEST": digest})
        probes = rb.safety_probes()
        safety.append(
            {"bundle_digest": digest, "failed": [p["check"] for p in probes if not p["ok"]]}
        )
        if safety[-1]["failed"]:
            raise SystemExit(f"faults: safety probes failed: {safety[-1]['failed']}")
        rb.deploy_tee(digest[:15], forward=True, password=password)
        agent = rb.Agent()
        try:
            for e in sorted(block, key=lambda x: by_ref[x.query_ref].order):
                task = by_ref[e.query_ref]
                log(f"task {task.name}: {e.kind or 'clean'}")
                _arm(task, e, variants)
                t0 = time.monotonic()
                result = rb.run_task(
                    task, agent, build["image"], float(rcfg["approval_timeout_ms"]) / 1000
                )
                seconds = round(time.monotonic() - t0, 1)
                _disarm()
                lines = _preload_lines(task)
                needles |= set(result.pop("_needles"))
                result.pop("_alert", None)
                rec = _record(e, task, lines, state["run_id"], digest, baseline)
                outcome: dict[str, Any] = {
                    "query_ref": e.query_ref,
                    "kind": e.kind,
                    "seconds": seconds,
                    "outcome": result["status"],
                    "steps": [s["action"] for s in result["steps"]],
                    "runbook": result["runbook"],
                    "llm_calls": len(lines),
                    "marked_spans": sum(1 for x in lines if x.get("marker") == "set"),
                    "request": [x.get("request") for x in lines],
                }
                sent = {x.get("sent_digest") for x in lines}
                if sent != {digest}:
                    problems.append(
                        f"{task.name}: sent system prompt digest {sent}, block {digest}"
                    )
                if any(str(x.get("marker", "")).startswith("error") for x in lines):
                    problems.append(f"{task.name}: the shim refused a marker")
                if rec is None:
                    outcome["status"] = "no_trace"
                else:
                    ledger.append(d / "ledger.jsonl", rec)
                    outcome |= {"trace_id": rec.trace_id, "status": ledger.status(rec)}
                outcomes.append(outcome)
        finally:
            agent.close()
        time.sleep(rb.FLUSH_SECONDS)
        sessions.append(rb.read_tee())
    otlp = {"resourceSpans": [rs for s in sessions for rs in s["resourceSpans"]]}
    (d / "otlp.json").write_text(json.dumps(otlp), encoding="utf-8")
    (d / "outcomes.jsonl").write_text(
        "".join(json.dumps(o) + "\n" for o in outcomes), encoding="utf-8"
    )
    (d / "needles.json").write_text(json.dumps(sorted(needles)), encoding="utf-8")
    state["run"] = {
        "nats_seq_before": seq,
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.monotonic() - started, 1),
        "spans_sent": len(spans_of(otlp)),
        "eval_tenants": tenants,
        "model": {
            "name": common.CHAT_MODEL,
            "digest": common.CHAT_MODEL_DIGEST,
            "server_version": server_version,
        },
        "safety": safety,
        "problems": problems,
    }
    faultslib.save_state(d, state)
    return 0


def record_phase(args: argparse.Namespace) -> int:
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    with faultslib.clickhouse_client() as client:
        state["record"] = record.record(client, ledger.read(d / "ledger.jsonl"))
    faultslib.save_state(d, state)
    log(f"recorded {state['record']}")
    return 0


def check_phase(args: argparse.Namespace) -> int:
    cfg = faultslib.config()
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    needles = set(json.loads((d / "needles.json").read_text(encoding="utf-8")))
    prompts = [triage_prompt(Path(state["app_dir"]), state["app_commit"])]
    prompts += [
        v.templates["system"]
        for v in scenario.load_all()[("prompt_variant", APP)].variants.values()
    ]
    windows = {p[i : i + 40] for p in prompts for i in range(0, max(len(p) - 40, 0) + 1, 10)}
    otlp = json.loads((d / "otlp.json").read_text(encoding="utf-8"))

    def content(rows: list[dict[str, Any]]) -> dict[str, Any]:
        alert = rb.leaks(needles, otlp, rows)
        prompt = rb.leaks(windows, otlp, rows)
        return {
            "alert_needles": len(needles),
            "prompt_windows": len(windows),
            "leaking": len(alert) + len(prompt),
        }

    result = faultslib.check_run(APP, d, state, cfg, golden=lambda _: (), content=content, log=log)
    result["problems"] += state["run"].get("problems", [])
    (d / "check.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"check: {len(result['problems'])} problems")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--app-dir", type=Path, required=True)
    b.add_argument("--run-id", help="default: faults-<date>-s<seed>-<commit7>")
    r = sub.add_parser("run")
    r.add_argument("--run-id", required=True)
    r.add_argument("--ollama-url")
    for name in ("record", "check"):
        sub.add_parser(name).add_argument("--run-id", required=True)
    args = parser.parse_args()
    return {"build": build, "run": run_phase, "record": record_phase, "check": check_phase}[
        args.command
    ](args)


if __name__ == "__main__":
    raise SystemExit(main())
