"""Faults injected into real runbook-agent runs in the kind sandbox (#14): malformed_output (model)
and prompt_variant (prompt), at the LLM seam, through the eval image's fetch preload.

The eval image is built FROM the capture image (tools/fixtures/capture_runbook_agent.py
build_image) and adds only packages/faults/preload/faults-preload.mjs and NODE_OPTIONS; the
app's own image and every non-eval manifest carry neither (tests/scripts/
test_faults_preload_guard.py, and the image checks below). The preload is a no-op unless
TRACEBLAME_FAULTS=1 and the tenant is an eval tenant.

Phases (only run, record, check and content touch kind; run them under
flock /tmp/traceblame-kind.lock -c '...'):

  build    (no kind)  capture image and eval image, image guard checks, the seeded plan of a
                      plan file (--plan; tools/faults/plans/, default the #14 validation.yaml).
                      For an existing run it verifies the plan and the eval image instead of
                      regenerating them
  run      (kind)     one attempt (#70): the eval image into kind, the sandbox reset, one task
                      at a time as tenant-eval for every plan entry without a ledger record;
                      before each task the driver arms /tmp/faults/armed.json in the agent pod,
                      after it reads the preload's ledger lines; the injector's ledger. A killed
                      attempt is resumed by running the phase again: it first reads the dead
                      attempt's tee and preload lines, then resets the sandbox, which ends a
                      task the dead driver left running
  record   (kind)     the ledger into eval_traces and injections, the abandoned traces into
                      eval_exclusions
  check    (kind)     as for agentic-rag (faultslib.check_run), content: alert text and prompts
  content  (kind)     only the content check of a finished run (read-only): every stored row
                      of its traces in every table with a trace_id, and the run's files

Every kind phase records the images kind ran and both databases' applied migrations at its
start and its end (provenance.py, #65); a change in between or an empty read is a problem.
The run phase also samples the eval pod after each deploy.
"""

import argparse
import contextlib
import hashlib
import json
import os
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
import provenance  # noqa: E402
import scenarios  # noqa: E402
from kindlib import (  # noqa: E402
    check_model,
    git,
    is_dirty,
    kubectl,
    require_clean,
    run,
    tenant_password,
)

from traceblame_faults import (  # noqa: E402
    appendlog,
    attempts,
    bundle,
    kinds,
    ledger,
    plan,
    querylist,
    record,
    scenario,
)

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


def excluded_tuples(cfg: dict[str, Any]) -> frozenset[str]:
    """The (scenario, parameters) tuples of the tasks in the plan's exclusion list."""
    listed = querylist.of_app(faultslib.committed_queries(cfg, "exclude"), APP)
    return frozenset(scenarios.ref_tuple(r.query_ref) for r in listed)


def _tasks(cfg: dict[str, Any]) -> list[Any]:
    """The plan file's tasks: the seeded plan's, or (distinct) tasks with distinct (scenario,
    parameters) tuples, none of them a tuple of the exclusion list."""
    seed, rcfg = int(cfg["seed"]), cfg["runbook_agent"]
    if rcfg["distinct"]:
        try:
            return scenarios.distinct_plan(
                seed,
                int(rcfg["per_scenario"]),
                scenarios=list(rcfg["scenarios"]),
                exclude=excluded_tuples(cfg),
            )
        except ValueError as exc:
            raise SystemExit(f"faults: {exc}") from exc
    wanted = set(rcfg["scenarios"])
    return [t for t in scenarios.plan(seed, int(rcfg["per_scenario"])) if t.scenario in wanted]


def _ref(task: Any, seed: int) -> str:
    return f"{task.name}@s{seed}"


def _plan_name(args: argparse.Namespace) -> str:
    """--plan, or the plan of the run being resumed, or the validation plan."""
    if args.plan is not None:
        return str(args.plan)
    if args.run_id is not None and (faultslib.WORK / args.run_id / faultslib.RUN_FILE).is_file():
        return faultslib.run_plan(faultslib.open_run(args.run_id))
    return faultslib.VALIDATION


def build_plan(
    cfg: dict[str, Any], run_id: str
) -> tuple[list[plan.Entry], list[plan.Query], list[querylist.Row]]:
    """The plan's entries (one task each), its queries with their split side, and its
    query-list rows. The split groups tasks by their (scenario, parameters) tuple and pairs
    them within their scenario and kind. Pure: needs no image and no cluster."""
    seed, rcfg = int(cfg["seed"]), cfg["runbook_agent"]
    tasks = _tasks(cfg)
    scenario_of = {_ref(t, seed): t.scenario for t in tasks}
    queries = [
        plan.Query(
            ref=_ref(t, seed), app=APP, source="runbook", stratum=t.scenario, golden_status="none"
        )
        for t in tasks
    ]
    try:
        entries = plan.plan(
            seed=seed,
            eval_run_id=run_id,
            app=APP,
            queries=queries,
            counts={k: int(n) for k, n in rcfg["counts"].items()},
            clean=int(rcfg["clean"]),
            scenarios=scenario.load_all(),
            exclusive=True,
        )
    except plan.PlanError as exc:
        raise SystemExit(f"faults: {exc}") from exc
    sides = faultslib.split_sides(
        cfg,
        entries,
        key=scenarios.ref_tuple,
        stratum=lambda ref, drawn: f"{scenario_of[ref]}/{'+'.join(drawn)}",
    )
    rows = querylist.build(APP, entries, sides)
    return entries, [q.with_(split=sides.get(q.ref, "")) for q in queries], rows


# build ------------------------------------------------------------------------------------


def build(args: argparse.Namespace) -> int:
    plan_name = _plan_name(args)
    cfg = faultslib.config(plan_name)
    rcfg = cfg["runbook_agent"]
    seed = int(cfg["seed"])
    app_dir: Path = args.app_dir.resolve()
    traceblame_commit = require_clean(ROOT)
    app_commit = require_clean(app_dir)
    dirty = is_dirty(ROOT)
    run_id = faultslib.ensure_run(
        args.run_id,
        seed=seed,
        commit=traceblame_commit,
        dirty=dirty,
        now=datetime.now(UTC),
        plan_name=plan_name,
    )
    d = faultslib.run_dir(run_id, APP)
    d.mkdir(parents=True, exist_ok=True)
    # The plan before the images: a plan that cannot be built or differs from the committed
    # query list stops here.
    entries, queries, rows = build_plan(cfg, run_id)
    compared = faultslib.verify_queries(cfg, APP, rows)
    text, queries_text = plan.dumps(entries), plan.dump_queries(queries)
    existing = faultslib.existing_plan(d, text, queries_text)
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
    # The config digest is the identifier kind's containerd shares with Docker (ADR-0009): the
    # run phase compares it with what the eval pod's node holds.
    eval_config_id = provenance.built_config_digest(eval_tag)
    baseline_text = triage_prompt(app_dir, app_commit)
    baseline = bundle.digest(APP, {"system": baseline_text})
    if existing:
        # An existing run is verified, never regenerated: the image built now must be the one
        # the run's first attempt was built with (the config digest, ADR-0009).
        before = faultslib.load_state(d)["build"]
        if (before.get("eval_config_id"), before["eval_image"]) != (eval_config_id, eval_tag):
            raise SystemExit(
                f"faults: run {run_id} was built with eval image {before['eval_image']} (config"
                f" {before.get('eval_config_id')}), this build gives {eval_tag} (config"
                f" {eval_config_id}): start a new run"
            )
        log(f"run {run_id} exists: its plan and eval image are verified, not rewritten")
        print(run_id)
        return 0
    (d / "plan.jsonl").write_text(text, encoding="utf-8")
    (d / "queries.jsonl").write_text(queries_text, encoding="utf-8")
    state: dict[str, Any] = {
        "run_id": run_id,
        "app": APP,
        "app_dir": str(app_dir),
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host_cpus": os.cpu_count(),
        "traceblame_commit": traceblame_commit,
        "dirty": dirty,
        "app_commit": app_commit,
        "app_dirty": is_dirty(app_dir),
        "provenance": {},  # images and migrations per kind phase (provenance.py, #65)
        "seed": seed,
        "config": rcfg,
        "build": built
        | {"eval_image": eval_tag, "eval_image_id": eval_id, "eval_config_id": eval_config_id},
        "image_guard": guard,
        "preload_sha256": common.sha256(PRELOAD),
        "baseline_digest": baseline,
        "plan": {
            "name": plan_name,
            "entries": len(entries),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "by_kind": dict(Counter(e.kind or "clean" for e in entries)),
        },
        "queries": {
            "queries": len(queries),
            "sha256": hashlib.sha256(queries_text.encode()).hexdigest(),
            "by_split": dict(Counter(q.split or "none" for q in queries)),
            "distinct_tuples": len({scenarios.ref_tuple(q.ref) for q in queries}),
            "excluded_tuples": len(excluded_tuples(cfg)),
            "compared_with_committed_list": compared,
        },
        "faults_sha256": faultslib.faults_sha256(Path(__file__).resolve()),
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
        "host_load1": ledger.host_load1(),  # the task has just ended
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


def _trace_ids(lines: list[dict[str, Any]]) -> list[str]:
    return sorted({str(x["trace_id"]) for x in lines if x.get("trace_id")})


def _reset_sandbox() -> None:
    """A clean slate before an attempt's first task, as the capture's, and the end of whatever
    a killed attempt left running: the preload disarmed, every task workload deleted,
    Alertmanager without earlier groups. Each block then redeploys the agent (a fresh pod, so
    a fresh /tmp/faults) and the tee."""
    # No agent pod yet (a first attempt), or it is going away: nothing to disarm.
    with contextlib.suppress(subprocess.CalledProcessError):
        _disarm()
    kubectl(
        "-n",
        rb.SANDBOX,
        "delete",
        "deployment,service,pod",
        "-l",
        f"{scenarios.LABEL}=workload",
        "--wait=true",
    )
    kubectl("-n", rb.MONITORING, "rollout", "restart", "deployment/alertmanager")
    kubectl("-n", rb.MONITORING, "rollout", "status", "deployment/alertmanager", "--timeout=300s")


def apply_oom_workloads(tasks: list[Any]) -> int:
    """The oom tasks' workloads, up front: their rule sees only pods at least ten minutes old.
    How many tasks it applied; nothing is applied when there is none (a plan of latency tasks,
    or a resume whose remaining entries have no oom task), since `kubectl apply` refuses an
    empty list."""
    oom = [t for t in tasks if t.scenario == "oom"]
    if oom:
        rb.apply([o for t in oom for o in scenarios.manifests(t, _python_image())])
    return len(oom)


def _fingerprint(
    state: dict[str, Any], snap: dict[str, Any], server_version: str
) -> dict[str, Any]:
    """What this attempt runs on, for the resume comparison (provenance.fingerprint)."""
    app_dir = Path(state["app_dir"])
    try:
        return provenance.fingerprint(
            snap,
            traceblame=(git(ROOT, "rev-parse", "HEAD"), is_dirty(ROOT)),
            app=(git(app_dir, "rev-parse", "HEAD"), is_dirty(app_dir)),
            plan_sha256=str(state["plan"]["sha256"]),
            queries_sha256=str(state.get("queries", {}).get("sha256", "")),
            faults_sha256=faultslib.faults_sha256(Path(__file__).resolve()),
            model={
                "name": common.CHAT_MODEL,
                "digest": common.CHAT_MODEL_DIGEST,
                "server_version": server_version,
            },
            preload_sha256=common.sha256(PRELOAD),
            eval_config_id=str(state["build"].get("eval_config_id")),
        )
    except provenance.FingerprintError as exc:
        raise SystemExit(f"faults: no provenance to run on: {exc}") from exc


def run_phase(args: argparse.Namespace) -> int:
    cfg = faultslib.run_config(args.run_id)
    rcfg = cfg["runbook_agent"]
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    seed = int(state["seed"])
    prov = provenance.begin(
        state,
        "run",
        kubectl=kubectl,
        log=log,
        eval_image=provenance.EvalImage(
            state["build"]["eval_image"],
            state["build"]["eval_image_id"],
            state["build"].get("eval_config_id"),
        ),
    )
    ollama = common.resolve_ollama(args.ollama_url)
    check_model(ollama)
    server_version = common.get_json(f"{ollama}/api/version")["version"]
    tenants = faultslib.require_eval_tenant()
    password = tenant_password(faultslib.EVAL_TENANT)
    entries = plan.loads((d / "plan.jsonl").read_text(encoding="utf-8"))
    by_entry = {e.entry_id: e for e in entries}
    sc = scenario.load_all()[("prompt_variant", APP)]
    variants = {n: v.templates["system"] for n, v in sc.variants.items()}
    by_ref = {_ref(t, seed): t for t in _tasks(cfg)}
    baseline = str(state["baseline_digest"])

    def salvage(pending: Any) -> dict[str, list[str]]:
        """The trace ids of the tasks in flight, from the agent pod's preload ledger, before
        the sandbox is reset. A pod that is gone gives none: the orphan sweep finds them."""
        found: dict[str, list[str]] = {}
        for started in pending:
            task = by_ref[by_entry[started.entry_id].query_ref]
            try:
                found[started.entry_id] = _trace_ids(_preload_lines(task))
            except subprocess.CalledProcessError:
                log(f"resume: no preload lines for {task.name} (the agent pod is gone)")
        return found

    attempt = faultslib.open_attempt(
        d,
        entries,
        state["run_id"],
        _fingerprint(state, prov.start, server_version),
        salvage=salvage,
        nats_seq_before=faultslib.stream_last_seq(),
    )
    rec = attempt.recovery
    if rec.killed or rec.lost or rec.torn:
        log(
            f"resume: attempts killed {list(rec.killed)}, {len(rec.lost)} entries in flight run"
            f" again, {len(rec.abandoned)} traces abandoned, torn ledger tail: {rec.torn}"
        )
    log(f"attempt {attempt.id}: {len(attempt.remaining)} of {len(entries)} entries to run")
    for dead in rec.killed:
        # The dead attempt's last tee session, before the tee is redeployed.
        try:
            salvaged = rb.read_tee()
        except subprocess.CalledProcessError:
            log(f"resume: no tee to read for {dead} (its spans are checked from the store only)")
            continue
        (d / f"otlp.{dead}.salvaged.json").write_text(json.dumps(salvaged), encoding="utf-8")
    started = time.monotonic()
    status = "completed"
    safety: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        if attempt.remaining:
            _run_tasks(
                attempt, d, state, rcfg, by_ref, variants, baseline, tenants, password, prov,
                safety, problems,
            )  # fmt: skip
    except BaseException as exc:
        status = f"failed: {type(exc).__name__}"
        raise
    finally:
        attempt.note(
            status=status,
            seconds=round(time.monotonic() - started, 1),
            safety=safety,
            problems=problems,
            provenance_start=prov.start,
        )
        attempt.end(status)
    notes = faultslib.attempt_notes(d)
    state["run"] = {
        "nats_seq_before": faultslib.nats_seq_before(d, state),
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.monotonic() - started, 1),
        "spans_sent": faultslib.exported_spans(faultslib.read_otlp(d)),
        "eval_tenants": tenants,
        "model": {
            "name": common.CHAT_MODEL,
            "digest": common.CHAT_MODEL_DIGEST,
            "server_version": server_version,
        },
        "safety": [x for note in notes for x in note.get("safety", [])],
        "problems": [x for note in notes for x in note.get("problems", [])],
        "attempts": len(attempts.attempts(faultslib.run_events(d))),
        "last_attempt": attempt.id,
    }
    prov.eval_pod("end")
    prov.end()
    faultslib.save_state(d, state)
    return 0


def _run_tasks(
    attempt: Any,
    d: Path,
    state: dict[str, Any],
    rcfg: dict[str, Any],
    by_ref: dict[str, Any],
    variants: dict[str, str],
    baseline: str,
    tenants: str,
    password: str,
    prov: Any,
    safety: list[dict[str, Any]],
    problems: list[str],
) -> None:
    """The attempt's remaining entries, one task at a time, block by block of bundle digest.
    Everything a task leaves is written when the task ends: its ledger record, its outcome
    line, its content needles; each block's tee copy when the block ends."""
    entry_log = attempt.entry_log()
    blocks: dict[str, list[plan.Entry]] = {baseline: []}
    for e in attempt.remaining:
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
    _reset_sandbox()
    used = [by_ref[e.query_ref] for e in attempt.remaining]
    names = scenarios.check_names(
        [o for t in used for o in scenarios.manifests(t, _python_image())]
    )
    if names:
        raise SystemExit(f"faults: workload names: {names}")
    apply_oom_workloads(used)
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
    outcomes = d / f"outcomes.{attempt.id}.jsonl"
    needles = d / f"needles.{attempt.id}.jsonl"
    for n, (digest, block) in enumerate(blocks.items()):
        if not block:
            continue
        rb.deploy_agent(build, base_env | {"TRACEBLAME_BUNDLE_DIGEST": digest})
        prov.eval_pod(digest[:15])  # the pod that runs this block's tasks, as kind ran it
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
                entry_log.start(e)
                _arm(task, e, variants)
                t0 = time.monotonic()
                result = rb.run_task(
                    task, agent, build["image"], float(rcfg["approval_timeout_ms"]) / 1000
                )
                seconds = round(time.monotonic() - t0, 1)
                _disarm()
                lines = _preload_lines(task)
                for trace_id in _trace_ids(lines):
                    entry_log.trace(e, trace_id)
                appendlog.append(needles, json.dumps(sorted(result.pop("_needles"))))
                result.pop("_alert", None)
                rec = _record(e, task, lines, state["run_id"], digest, baseline)
                outcome: dict[str, Any] = {
                    "entry_id": e.entry_id,
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
                    # No trace, or more than one: the entry stays incomplete, a resume runs it
                    # again, and whatever traces it left are abandoned.
                    entry_log.no_record(e, "no_trace" if not _trace_ids(lines) else "ambiguous")
                    outcome["status"] = "no_trace"
                else:
                    rec = entry_log.record(e, rec)
                    outcome |= {"trace_id": rec.trace_id, "status": ledger.status(rec)}
                appendlog.append(outcomes, json.dumps(outcome, sort_keys=True))
                attempt.note(safety=safety, problems=problems)
        finally:
            agent.close()
        time.sleep(rb.FLUSH_SECONDS)
        (d / f"otlp.{attempt.id}.b{n}.json").write_text(json.dumps(rb.read_tee()), encoding="utf-8")


def record_phase(args: argparse.Namespace) -> int:
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    prov = provenance.begin(state, "record", kubectl=kubectl, log=log)
    abandoned = attempts.abandoned(faultslib.run_events(d))
    with faultslib.clickhouse_client() as client:
        state["record"] = record.record(client, ledger.read(d / "ledger.jsonl"), abandoned)
    prov.end()
    faultslib.save_state(d, state)
    log(f"recorded {state['record']}")
    return 0


def _content(d: Path, state: dict[str, Any]) -> faultslib.Content:
    """The #13 needles (alert text windows and task-unique label values) and 40-character
    windows of every triage prompt the run could send, against every stored row of the run and
    the run's files."""
    needles = faultslib.read_needles(d)
    prompts = [triage_prompt(Path(state["app_dir"]), state["app_commit"])]
    prompts += [
        v.templates["system"]
        for v in scenario.load_all()[("prompt_variant", APP)].variants.values()
    ]
    windows = {p[i : i + 40] for p in prompts for i in range(0, max(len(p) - 40, 0) + 1, 10)}

    def content(tables: dict[str, list[dict[str, Any]]], files: list[Path]) -> dict[str, Any]:
        docs = [faultslib.json_docs(f) for f in files]  # parsed: needles hold "\n" and quotes
        alert = rb.leaks(needles, tables, docs)
        prompt = rb.leaks(windows, tables, docs)
        return {
            "needles": {"alert": len(needles), "prompt_windows": len(windows)},
            "leaking": len(alert) + len(prompt),
        }

    return content


def check_phase(args: argparse.Namespace) -> int:
    cfg = faultslib.run_config(args.run_id)
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    prov = provenance.begin(state, "check", kubectl=kubectl, log=log)
    result = faultslib.check_run(
        APP, d, state, cfg, golden=lambda _: (), content=_content(d, state), log=log
    )
    result["problems"] += state["run"].get("problems", [])
    (d / "check.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    prov.end()
    faultslib.save_state(d, state)
    log(f"check: {len(result['problems'])} problems")
    return 0


def content_phase(args: argparse.Namespace) -> int:
    d = faultslib.run_dir(args.run_id, APP)
    state = faultslib.load_state(d)
    prov = provenance.begin(state, "content", kubectl=kubectl, log=log)
    check = faultslib.recheck_content(d, _content(d, state), log)
    prov.end()
    faultslib.save_state(d, state)
    log(f"content: {check['content_check']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--app-dir", type=Path, required=True)
    b.add_argument("--run-id", help="default: a new run (tools/faults/run_id.py new)")
    b.add_argument("--plan", help="a plan file's name (tools/faults/plans/); default validation")
    r = sub.add_parser("run")
    r.add_argument("--run-id", required=True)
    r.add_argument("--ollama-url")
    for name in ("record", "check", "content"):
        sub.add_parser(name).add_argument("--run-id", required=True)
    args = parser.parse_args()
    phases = {
        "build": build,
        "run": run_phase,
        "record": record_phase,
        "check": check_phase,
        "content": content_phase,
    }
    return phases[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
