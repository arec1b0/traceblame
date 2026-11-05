"""Faults injected into real agentic-rag runs on CUAD, exported through kind (#14).

Phases, each its own command so that only the ones that touch kind run under the shared lock
(flock /tmp/traceblame-kind.lock -c '...'):

  prepare  (no kind)   capture venv with traceblame-faults, CUAD queries with golden chunks,
                       the seeded plan; .tools/faults/<run>/agentic-rag/
  run      (kind)      every plan entry through the real app as tenant-eval, exported by the
                       pinned collector to the kind agent; the injector's ledger
  record   (kind)      the ledger into eval_traces and injections (traceblame-faults record)
  check    (kind)      stored spans, markers, the join, the signal per kind against the clean
                       trace of the same query, trace_summary and trace.completed, content
  content  (kind)      only the content check of a finished run (read-only): every stored row
                       of its traces in every table with a trace_id, and the run's files

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
from kindlib import PortForward, check_model, require_clean, run, spans_of, tenant_password  # noqa: E402

from traceblame_faults import kinds, ledger, plan, record  # noqa: E402

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


def _app(python: Path, env: dict[str, str], *args: str, out: Path | None = None) -> str:
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
    if out is not None:
        out.write_text(result.stdout, encoding="utf-8")
    return result.stdout


# prepare ----------------------------------------------------------------------------------


def prepare(args: argparse.Namespace) -> int:
    cfg = faultslib.config()
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
    run_id = faultslib.ensure_run(
        args.run_id, seed=seed, commit=traceblame_commit, now=datetime.now(UTC)
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
        "app_commit": app_commit,
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
    sample = ["--archive", str(archive), "--n", str(rcfg["n"]), "--seed", str(seed)]
    sample += ["--ratio", str(rcfg["ratio"]), "--stratum", "answerable"]
    every = plan.load_queries(_app(python, _env(state), "queries", *sample))
    state["golden_coverage"] = dict(Counter(q.golden_status for q in every)) | {
        "answerable_sampled": len(every)
    }
    queries = [q for q in every if q.golden_status == "mapped"][: int(rcfg["queries"])]
    if len(queries) < int(rcfg["queries"]):
        raise SystemExit(f"faults: {len(queries)} mapped queries, {rcfg['queries']} wanted")
    (d / "queries.jsonl").write_text(plan.dump_queries(queries), encoding="utf-8")
    from traceblame_faults import scenario

    n = len(queries)
    entries = plan.plan(
        seed=seed,
        eval_run_id=run_id,
        app=APP,
        queries=queries,
        counts={k: n for k, kind in kinds.KINDS.items() if APP in kind.apps},
        clean=n,
        scenarios=scenario.load_all(),
    )
    text = plan.dumps(entries)
    (d / "plan.jsonl").write_text(text, encoding="utf-8")
    state["plan"] = {
        "entries": len(entries),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "by_kind": dict(Counter(e.kind or "clean" for e in entries)),
    }
    state["faults_sha256"] = {
        p.relative_to(ROOT).as_posix(): common.sha256(p)
        for p in sorted((ROOT / "packages" / "faults" / "src").rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    } | {APP_SCRIPT.relative_to(ROOT).as_posix(): common.sha256(APP_SCRIPT)}
    _save(d, state)
    print(run_id)
    return 0


# run --------------------------------------------------------------------------------------


def run_phase(args: argparse.Namespace) -> int:
    d = _dir(args.run_id)
    state = _state(d)
    tenants = faultslib.require_eval_tenant()
    password = tenant_password(faultslib.EVAL_TENANT)
    python = rag.VENV / "bin" / "python"
    (d / "ledger.jsonl").unlink(missing_ok=True)
    seq = faultslib.stream_last_seq()
    started = time.monotonic()
    forward = PortForward("svc/otel-agent", 4318)
    try:
        col = rag.Collector(
            d, "otlp", agent_port=forward.port, tenant=(faultslib.EVAL_TENANT, password)
        )
        try:
            log(f"{state['plan']['entries']} entries through the app")
            _app(
                python,
                _env(state, tenants),
                "run",
                "--archive", state["archive"],
                "--plan", str(d / "plan.jsonl"),
                "--queries", str(d / "queries.jsonl"),
                "--ledger", str(d / "ledger.jsonl"),
                out=d / "outcomes.jsonl",
            )  # fmt: skip
        finally:
            otlp = col.stop()
    finally:
        forward.close()
    (d / "otlp.json").write_text(json.dumps(otlp), encoding="utf-8")
    state["run"] = {
        "nats_seq_before": seq,
        "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": round(time.monotonic() - started, 1),
        "spans_sent": len(spans_of(otlp)),
        "eval_tenants": tenants,
    }
    _save(d, state)
    return 0


# record -----------------------------------------------------------------------------------


def record_phase(args: argparse.Namespace) -> int:
    d = _dir(args.run_id)
    state = _state(d)
    records = ledger.read(d / "ledger.jsonl")
    with faultslib.clickhouse_client() as client:
        counts = record.record(client, records)
    state["record"] = counts
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
    cfg = faultslib.config()
    d = _dir(args.run_id)
    state = _state(d)
    queries = _queries(d)
    result = faultslib.check_run(
        APP,
        d,
        state,
        cfg,
        golden=lambda ref: queries[ref].golden_doc_ids,
        content=_content(d, state, sorted(queries)),
        log=log,
    )
    log(f"check: {len(result['problems'])} problems")
    return 0


def content_phase(args: argparse.Namespace) -> int:
    d = _dir(args.run_id)
    state = _state(d)
    check = faultslib.recheck_content(d, _content(d, state, sorted(_queries(d))), log)
    log(f"content: {check['content_check']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--app-dir", type=Path, required=True)
    p.add_argument("--ollama-url")
    p.add_argument("--run-id", help="default: a new run (tools/faults/run_id.py new)")
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
