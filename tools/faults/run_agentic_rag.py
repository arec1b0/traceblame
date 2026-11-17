"""Faults injected into real agentic-rag runs on CUAD, exported through kind (#14).

Phases, each its own command so that only the ones that touch kind run under the shared lock
(flock /tmp/traceblame-kind.lock -c '...'):

  prepare  (no kind)   capture venv with traceblame-faults, CUAD queries with golden chunks,
                       the seeded plan of a plan file (--plan; tools/faults/plans/, default the
                       #14 validation.yaml); .tools/faults/<run>/agentic-rag/. For an existing
                       run it verifies the plan instead of regenerating it
  run      (kind)      one attempt (#70): every plan entry without a ledger record through the
                       real app as tenant-eval, exported by the pinned collector to the kind
                       agent; the injector's ledger. A killed attempt is resumed by running the
                       phase again: finished entries are skipped, the trace in flight is
                       recorded as abandoned, a changed provenance is refused
  record   (kind)      the ledger into eval_traces and injections, the abandoned traces into
                       eval_exclusions (traceblame-faults record)
  check    (kind)      stored spans, markers, the join, the signal per kind against the clean
                       trace of the same query, trace_summary and trace.completed, content
  content  (kind)      only the content check of a finished run (read-only): every stored row
                       of its traces in every table with a trace_id, and the run's files

Every kind phase records the images kind ran and both databases' applied migrations at its
start and its end (provenance.py, #65); a change in between or an empty read is a problem.

The report over both apps is tools/faults/report.py. Run from WSL2 or Linux after
`make kind-up kind-deploy`, with both checkouts committed (provenance).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fixtures"))
sys.path.insert(0, str(HERE.parent / "fixtures" / "agentic_rag"))
sys.path.insert(0, str(HERE))

import capture as common  # noqa: E402
import capture_agentic_rag as rag  # noqa: E402
import cuad  # noqa: E402
import faultslib  # noqa: E402
import provenance  # noqa: E402
from kindlib import (  # noqa: E402
    PortForward,
    check_model,
    git,
    is_dirty,
    kubectl,
    require_clean,
    run,
    tenant_password,
)

from traceblame_faults import attempts, ledger, plan, querylist, record  # noqa: E402

ROOT = common.ROOT
APP = "agentic-rag"
APP_SCRIPT = HERE / "agentic_rag_app.py"


def log(msg: str) -> None:
    print(f"faults[{APP}]: {msg}", file=sys.stderr, flush=True)


def _dir(run_id: str) -> Path:
    return faultslib.run_dir(run_id, APP)


_state = faultslib.load_state
_save = faultslib.save_state


def _env(state: dict[str, Any], tenants: str = "[tenant-eval]") -> dict[str, str]:
    return rag.app_env(
        Path(state["app_dir"]),
        state["ollama"],
        Path(state["embedding"]),
        4318,
        TRACEBLAME_TENANT_ID=faultslib.EVAL_TENANT,
        TRACEBLAME_EVAL_RUN_ID=state["run_id"],
        TRACEBLAME_FAULTS="1",
        TRACEBLAME_EVAL_TENANTS=tenants,
    )


def _app(python: Path, env: dict[str, str], *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        [str(python), "-I", str(APP_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-4000:])  # error levels only, no messages (quiet_logs)
        raise SystemExit(f"faults: agentic_rag_app.py {args[0]} exited {result.returncode}")
    return result.stdout


def _plan_name(args: argparse.Namespace) -> str:
    """--plan, or the plan of the run being resumed, or the validation plan."""
    if args.plan is not None:
        return str(args.plan)
    if args.run_id is not None and (faultslib.WORK / args.run_id / faultslib.RUN_FILE).is_file():
        return faultslib.run_plan(faultslib.open_run(args.run_id))
    return faultslib.VALIDATION


def sample_queries(
    cfg: dict[str, Any], python: Path, env: dict[str, str], archive: Path
) -> list[plan.Query]:
    """The plan file's sampled answerable CUAD questions, each with its golden chunks mapped by
    the app's own chunker (agentic_rag_app.py queries, in the capture environment)."""
    rcfg = cfg["agentic_rag"]
    sample = ["--archive", str(archive), "--n", str(rcfg["n"]), "--seed", str(cfg["seed"])]
    sample += ["--ratio", str(rcfg["ratio"]), "--stratum", "answerable"]
    return plan.load_queries(_app(python, env, "queries", *sample))


def select_queries(
    cfg: dict[str, Any], every: list[plan.Query]
) -> tuple[list[plan.Query], dict[str, int]]:
    """The plan's queries of the sampled ones: mapped golden chunks, not in the plan's exclusion
    list, the first `queries` of them. Also how many the exclusion list removed."""
    rcfg = cfg["agentic_rag"]
    listed = faultslib.committed_queries(cfg, "exclude")
    excluded = {r.query_ref for r in querylist.of_app(listed, APP)}
    if listed and not excluded:
        raise SystemExit(f"faults: the exclusion list has no {APP} query")
    mapped = [q for q in every if q.golden_status == "mapped"]
    kept = [q for q in mapped if q.ref not in excluded]
    queries = kept[: int(rcfg["queries"])]
    if len(queries) < int(rcfg["queries"]):
        raise SystemExit(
            f"faults: {len(queries)} mapped queries, {rcfg['queries']} wanted ({len(every)}"
            f" answerable sampled, {len(mapped)} mapped, {len(mapped) - len(kept)} excluded)"
        )
    return queries, {"listed": len(excluded), "removed": len(mapped) - len(kept)}


def build_plan(
    cfg: dict[str, Any], run_id: str, queries: list[plan.Query]
) -> tuple[list[plan.Entry], list[plan.Query], list[querylist.Row]]:
    """The plan's entries, its queries with their split side, and its query-list rows."""
    from traceblame_faults import scenario

    rcfg = cfg["agentic_rag"]
    try:
        entries = plan.plan(
            seed=int(cfg["seed"]),
            eval_run_id=run_id,
            app=APP,
            queries=queries,
            counts={k: int(n) for k, n in rcfg["counts"].items()},
            clean=int(rcfg["clean"]),
            scenarios=scenario.load_all(),
        )
    except plan.PlanError as exc:
        raise SystemExit(f"faults: {exc}") from exc
    sides = faultslib.split_sides(cfg, entries)
    rows = querylist.build(APP, entries, sides)
    used = {e.query_ref for e in entries}
    return entries, [q.with_(split=sides.get(q.ref, "")) for q in queries if q.ref in used], rows


# prepare ----------------------------------------------------------------------------------


def prepare(args: argparse.Namespace) -> int:
    plan_name = _plan_name(args)
    cfg = faultslib.config(plan_name)
    rcfg = cfg["agentic_rag"]
    seed = int(cfg["seed"])
    app_dir: Path = args.app_dir.resolve()
    traceblame_commit = require_clean(ROOT)
    app_commit = require_clean(app_dir)
    uv = os.environ.get("UV", "uv")
    ollama = common.resolve_ollama(args.ollama_url)
    server_version = common.get_json(f"{ollama}/api/version")["version"]
    check_model(ollama)
    python = rag.ensure_venv(uv)
    env = os.environ | {"VIRTUAL_ENV": str(rag.VENV)}
    run(
        [uv, "pip", "install", "--quiet", "--no-deps", "-e", str(ROOT / "packages" / "faults")],
        env=env,
    )
    packages = rag.installed_versions(python)
    embedding = rag.ensure_embedding_model(python)
    archive = cuad.ensure_archive(rag.CACHE / "cuad")
    dirty = is_dirty(ROOT)
    run_id = faultslib.ensure_run(
        args.run_id,
        seed=seed,
        commit=traceblame_commit,
        dirty=dirty,
        now=datetime.now(UTC),
        plan_name=plan_name,
    )
    d = _dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {
        "run_id": run_id,
        "app": APP,
        "app_dir": str(app_dir),
        "prepared_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host_cpus": os.cpu_count(),
        "traceblame_commit": traceblame_commit,
        "dirty": dirty,
        "app_commit": app_commit,
        "app_dirty": is_dirty(app_dir),
        "provenance": {},  # images and migrations per kind phase (provenance.py, #65)
        "ollama": ollama,
        "model": {
            "name": common.CHAT_MODEL,
            "digest": common.CHAT_MODEL_DIGEST,
            "server": "ollama",
            "server_version": server_version,
        },
        "packages": packages,
        "embedding": str(embedding),
        "archive": str(archive),
        "seed": seed,
        "config": rcfg,
    }
    log("CUAD queries and golden chunks")
    every = sample_queries(cfg, python, _env(state), archive)
    state["golden_coverage"] = dict(Counter(q.golden_status for q in every)) | {
        "answerable_sampled": len(every)
    }
    selected, state["excluded_queries"] = select_queries(cfg, every)
    entries, queries, rows = build_plan(cfg, run_id, selected)
    compared = faultslib.verify_queries(cfg, APP, rows)
    text, queries_text = plan.dumps(entries), plan.dump_queries(queries)
    if faultslib.existing_plan(d, text, queries_text):
        log(f"run {run_id} exists: its plan is verified ({len(entries)} entries), not rewritten")
        print(run_id)
        return 0
    (d / "queries.jsonl").write_text(queries_text, encoding="utf-8")
    (d / "plan.jsonl").write_text(text, encoding="utf-8")
    state["plan"] = {
        "name": plan_name,
        "entries": len(entries),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "by_kind": dict(Counter(e.kind or "clean" for e in entries)),
    }
    state["queries"] = {
        "queries": len(queries),
        "sha256": hashlib.sha256(queries_text.encode()).hexdigest(),
        "by_split": dict(Counter(q.split or "none" for q in queries)),
        "compared_with_committed_list": compared,
    }
    state["faults_sha256"] = faultslib.faults_sha256(APP_SCRIPT)
    _save(d, state)
    print(run_id)
    return 0


# run --------------------------------------------------------------------------------------


def _fingerprint(state: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any]:
    """What this attempt runs on, for the resume comparison (provenance.fingerprint)."""
    app_dir = Path(state["app_dir"])
    ollama = str(state["ollama"])
    check_model(ollama)
    try:
        return provenance.fingerprint(
            snap,
            traceblame=(git(ROOT, "rev-parse", "HEAD"), is_dirty(ROOT)),
            app=(git(app_dir, "rev-parse", "HEAD"), is_dirty(app_dir)),
            plan_sha256=str(state["plan"]["sha256"]),
            queries_sha256=str(state.get("queries", {}).get("sha256", "")),
            faults_sha256=faultslib.faults_sha256(APP_SCRIPT),
            model={
                "name": common.CHAT_MODEL,
                "digest": common.CHAT_MODEL_DIGEST,
                "server_version": str(common.get_json(f"{ollama}/api/version")["version"]),
            },
        )
    except provenance.FingerprintError as exc:
        raise SystemExit(f"faults: no provenance to run on: {exc}") from exc


def run_phase(args: argparse.Namespace) -> int:
    d = _dir(args.run_id)
    state = _state(d)
    tenants = faultslib.require_eval_tenant()
    password = tenant_password(faultslib.EVAL_TENANT)
    prov = provenance.begin(state, "run", kubectl=kubectl, log=log)
    python = rag.VENV / "bin" / "python"
    entries = plan.loads((d / "plan.jsonl").read_text(encoding="utf-8"))
    attempt = faultslib.open_attempt(
        d,
        entries,
        state["run_id"],
        _fingerprint(state, prov.start),
        nats_seq_before=faultslib.stream_last_seq(),
    )
    rec = attempt.recovery
    if rec.killed or rec.lost or rec.torn:
        log(
            f"resume: attempts killed {list(rec.killed)}, {len(rec.lost)} entries in flight run"
            f" again, {len(rec.abandoned)} traces abandoned, torn ledger tail: {rec.torn}"
        )
    log(f"attempt {attempt.id}: {len(attempt.remaining)} of {len(entries)} entries to run")
    started = time.monotonic()
    status = "completed"
    try:
        if attempt.remaining:
            forward = PortForward("svc/otel-agent", 4318)
            try:
                attempt.helper("port-forward", forward._proc.pid)  # pyright: ignore[reportPrivateUsage]
                col = rag.Collector(
                    d,
                    f"otlp.{attempt.id}",
                    agent_port=forward.port,
                    tenant=(faultslib.EVAL_TENANT, password),
                )
                try:
                    attempt.helper("collector", col._proc.pid)  # pyright: ignore[reportPrivateUsage]
                    _run_app(python, _env(state, tenants), d, attempt.id, attempt.lock_fd, state)
                finally:
                    col.stop()
            finally:
                forward.close()
    except BaseException as exc:
        status = f"failed: {type(exc).__name__}"
        raise
    finally:
        attempt.note(
            status=status,
            seconds=round(time.monotonic() - started, 1),
            eval_tenants=tenants,
            provenance_start=prov.start,
        )
        attempt.end(status)
    events = faultslib.run_events(d)
    state["run"] = {
        "nats_seq_before": faultslib.nats_seq_before(d, state),
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.monotonic() - started, 1),
        "spans_sent": faultslib.exported_spans(faultslib.read_otlp(d)),
        "eval_tenants": tenants,
        "attempts": len(attempts.attempts(events)),
        "last_attempt": attempt.id,
    }
    prov.end()
    _save(d, state)
    return 0


def _run_app(
    python: Path,
    env: dict[str, str],
    d: Path,
    attempt_id: str,
    lock_fd: int,
    state: dict[str, Any],
) -> None:
    """The attempt's entries through the app. The app inherits the run lock, so it blocks a
    resume for as long as it lives, also when this driver is killed alone; its output goes to a
    file, not a pipe, so it survives that too."""
    out = d / f"app.{attempt_id}.log"
    with out.open("ab") as sink:
        result = subprocess.run(  # noqa: S603 - fixed argv
            [
                str(python), "-I", str(APP_SCRIPT), "run",
                "--archive", state["archive"],
                "--plan", str(d / "plan.jsonl"),
                "--queries", str(d / "queries.jsonl"),
                "--run-dir", str(d),
                "--attempt-id", attempt_id,
            ],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
            pass_fds=(lock_fd,),
            check=False,
        )  # fmt: skip
    if result.returncode != 0:
        # Error levels only, no messages (quiet_logs).
        sys.stderr.write(out.read_text(encoding="utf-8", errors="replace")[-4000:])
        raise SystemExit(f"faults: agentic_rag_app.py run exited {result.returncode}")


# record -----------------------------------------------------------------------------------


def record_phase(args: argparse.Namespace) -> int:
    d = _dir(args.run_id)
    state = _state(d)
    prov = provenance.begin(state, "record", kubectl=kubectl, log=log)
    records = ledger.read(d / "ledger.jsonl")
    abandoned = attempts.abandoned(faultslib.run_events(d))
    with faultslib.clickhouse_client() as client:
        counts = record.record(client, records, abandoned)
    state["record"] = counts
    prov.end()
    _save(d, state)
    log(f"recorded {counts}")
    return 0


# check ------------------------------------------------------------------------------------


def _content(d: Path, state: dict[str, Any], qids: list[str]) -> faultslib.Content:
    """The #12 needles (each query's question and every 48-character window of every chunk of
    its contract) against every stored row of the run and the run's files."""

    def content(tables: dict[str, list[dict[str, Any]]], files: list[Path]) -> dict[str, Any]:
        dump = d / "stored.check.json"
        dump.write_text(json.dumps(tables, default=str), encoding="utf-8")
        result = subprocess.run(  # noqa: S603 - fixed argv
            [
                str(rag.VENV / "bin" / "python"), "-I", str(rag.RUN_SCRIPT), "check-content",
                "--archive", state["archive"], "--qids", *qids,
                "--files", *map(str, files), str(dump),
            ],
            capture_output=True, text=True, env=_env(state), check=False,
        )  # fmt: skip
        out = cast("dict[str, Any]", json.loads(result.stdout.splitlines()[-1]))
        return {
            "needles": {
                "queries": out["checked_qids"],
                "contract_chunks": out["chunks"],
                "windows": out["windows"],
            },
            "leaking": len(out["leaking_qids"]),
        }

    return content


def _queries(d: Path) -> dict[str, plan.Query]:
    text = (d / "queries.jsonl").read_text(encoding="utf-8")
    return {q.ref: q for q in plan.load_queries(text)}


def check_phase(args: argparse.Namespace) -> int:
    cfg = faultslib.run_config(args.run_id)
    d = _dir(args.run_id)
    state = _state(d)
    queries = _queries(d)
    prov = provenance.begin(state, "check", kubectl=kubectl, log=log)
    result = faultslib.check_run(
        APP,
        d,
        state,
        cfg,
        golden=lambda ref: queries[ref].golden_doc_ids,
        content=_content(d, state, sorted(queries)),
        log=log,
    )
    prov.end()
    _save(d, state)
    log(f"check: {len(result['problems'])} problems")
    return 0


def content_phase(args: argparse.Namespace) -> int:
    d = _dir(args.run_id)
    state = _state(d)
    prov = provenance.begin(state, "content", kubectl=kubectl, log=log)
    check = faultslib.recheck_content(d, _content(d, state, sorted(_queries(d))), log)
    prov.end()
    _save(d, state)
    log(f"content: {check['content_check']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--app-dir", type=Path, required=True)
    p.add_argument("--ollama-url")
    p.add_argument("--run-id", help="default: a new run (tools/faults/run_id.py new)")
    p.add_argument("--plan", help="a plan file's name (tools/faults/plans/); default validation")
    for name in ("run", "record", "check", "content"):
        sub.add_parser(name).add_argument("--run-id", required=True)
    args = parser.parse_args()
    phases = {
        "prepare": prepare,
        "run": run_phase,
        "record": record_phase,
        "check": check_phase,
        "content": content_phase,
    }
    return phases[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
