"""The committed query lists of a plan with a split (#70): tools/faults/plans/.

  generate  writes validation.queries.jsonl (every candidate query of the #14 validation run:
            D1 may use none of them), <plan>.queries.jsonl (every query of the plan with its
            app, its split side and the kinds that drew it) and <plan>.queries.lock.json (what
            they were generated from, their sha256 and their counts)
  check     regenerates into a temporary directory and compares bytes with the committed
            files; reports how many queries it compared (zero = fail)

Ids only, never a question's or an alert's text. The same inputs give the same bytes: the
pinned CUAD archive, the agentic-rag checkout's chunker (it decides which questions have mapped
golden chunks), the plan file, the validation plan, this repository's fault library. The lock
records each of them. No kind, no model server; needs the capture environment (built on demand)
and both checkouts committed for `generate`.
"""

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "fixtures"))
sys.path.insert(0, str(HERE.parent / "fixtures" / "agentic_rag"))
sys.path.insert(0, str(HERE.parent / "fixtures" / "runbook_agent"))
sys.path.insert(0, str(HERE))

import capture_agentic_rag as rag  # noqa: E402
import cuad  # noqa: E402
import faultslib  # noqa: E402
import run_agentic_rag  # noqa: E402
import run_runbook_agent  # noqa: E402
import scenarios  # noqa: E402
from kindlib import git, require_clean, run  # noqa: E402

from traceblame_faults import planfile, querylist  # noqa: E402

ROOT = faultslib.ROOT
PLANS = faultslib.PLANS
RAG, RUNBOOK = "agentic-rag", "runbook-agent"
# Not compared by `check`: the commit the generator ran at and the digest of the fault library
# at that commit are a record of what generated the lists, not an input of their own. What
# they stand for is checked by the lists themselves regenerating byte for byte.
UNCOMPARED = ("generator_commit", "faults_sha256")


def log(msg: str) -> None:
    print(f"d1-queries: {msg}", file=sys.stderr, flush=True)


def pinned_uv() -> str:
    """The uv pinned in tools/versions.env (.tools/bin/uv), not whichever is first on PATH: an
    older global uv cannot build the fault library into the capture environment."""
    path = ROOT / ".tools" / "bin" / "uv"
    if not path.is_file():
        raise SystemExit(f"d1-queries: no pinned uv at {path}: run make bootstrap, or set UV")
    return str(path)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def validation_rows(archive: Path) -> list[querylist.Row]:
    """Every candidate query of the validation run: the answerable CUAD questions its sampling
    draws (it ran the first mapped ones of them), and its sandbox tasks."""
    cfg = faultslib.config(faultslib.VALIDATION)
    rcfg, seed = cfg["agentic_rag"], int(cfg["seed"])
    items = cuad.sample(
        cuad.load_test_split(archive), n=int(rcfg["n"]), seed=seed, ratio=float(rcfg["ratio"])
    )
    rows = [querylist.Row(RAG, i.qid, "", ()) for i in items if i.answerable]
    tasks = run_runbook_agent._tasks(cfg)  # pyright: ignore[reportPrivateUsage]
    rows += [querylist.Row(RUNBOOK, f"{t.name}@s{seed}", "", ()) for t in tasks]
    return rows


def _side_counts(rows: list[querylist.Row]) -> dict[str, Any]:
    kinds: dict[str, dict[str, int]] = {}
    for r in rows:
        for kind in r.kinds:
            kinds.setdefault(kind, {"calib": 0, "test": 0})[r.split] += 1
    return {
        "queries": len(rows),
        "by_split": dict(sorted(Counter(r.split for r in rows).items())),
        "kinds": dict(sorted(kinds.items())),
    }


def compute(plan_name: str, app_dir: Path, uv: str, *, committed: bool) -> dict[str, str]:
    """file name -> text of the plan's lists and lock. The plan's exclusion list is read from
    tools/faults/plans/ as the drivers read it, so `generate` writes it there first."""
    cfg = faultslib.config(plan_name)
    if "split" not in cfg or "queries" not in cfg or "exclude" not in cfg:
        raise SystemExit(f"d1-queries: plan {plan_name} has no split, query list or exclusion list")
    archive = cuad.ensure_archive(rag.CACHE / "cuad")
    out: dict[str, str] = {str(cfg["exclude"]): querylist.dumps(validation_rows(archive))}
    if committed:
        (PLANS / str(cfg["exclude"])).write_text(out[str(cfg["exclude"])], encoding="utf-8")
    elif (PLANS / str(cfg["exclude"])).read_text(encoding="utf-8") != out[str(cfg["exclude"])]:
        raise SystemExit(
            f"d1-queries: {cfg['exclude']} differs from what the validation plan gives"
        )

    log("CUAD questions and golden chunks through the app's chunker")
    python = rag.ensure_venv(uv)
    run(
        [uv, "pip", "install", "--quiet", "--no-deps", "-e", str(ROOT / "packages" / "faults")],
        env=os.environ | {"VIRTUAL_ENV": str(rag.VENV)},
    )
    env = rag.app_env(
        app_dir,
        "http://127.0.0.1:9",  # the query step calls no model
        rag.ensure_embedding_model(python),
        4318,
        TRACEBLAME_TENANT_ID=faultslib.EVAL_TENANT,
        TRACEBLAME_EVAL_RUN_ID="plan",
        TRACEBLAME_FAULTS="1",
        TRACEBLAME_EVAL_TENANTS=f"[{faultslib.EVAL_TENANT}]",
    )
    every = run_agentic_rag.sample_queries(cfg, python, env, archive)
    selected, excluded = run_agentic_rag.select_queries(cfg, every)
    _, _, rag_rows = run_agentic_rag.build_plan(cfg, "plan", selected)
    _, rb_queries, rb_rows = run_runbook_agent.build_plan(cfg, "plan")
    out[str(cfg["queries"])] = querylist.dumps([*rag_rows, *rb_rows])

    title = {i.qid: i.title for i in cuad.load_test_split(archive)}
    contracts: dict[str, set[str]] = {"calib": set(), "test": set()}
    for r in rag_rows:
        contracts[r.split].add(title[r.query_ref])
    tuples = {scenarios.ref_tuple(q.ref) for q in rb_queries}
    lock: dict[str, Any] = {
        "plan": plan_name,
        "plan_sha256": planfile.sha256(faultslib.plan_path(plan_name)),
        "validation_plan_sha256": planfile.sha256(faultslib.plan_path(faultslib.VALIDATION)),
        "cuad": {
            "commit": cuad.CUAD_COMMIT,
            "archive_sha256": cuad.CUAD_SHA256,
            "split_member": cuad.SPLIT_MEMBER,
        },
        "agentic_rag_commit": git(app_dir, "rev-parse", "HEAD"),
        "generator_commit": git(ROOT, "rev-parse", "HEAD"),
        "faults_sha256": _sha256(
            json.dumps(
                faultslib.faults_sha256(
                    Path(run_agentic_rag.__file__).resolve(),
                    Path(run_runbook_agent.__file__).resolve(),
                    Path(scenarios.__file__).resolve(),
                    Path(cuad.__file__).resolve(),
                ),
                sort_keys=True,
            )
        ),
        "files": {name: _sha256(text) for name, text in sorted(out.items())},
        "counts": {RAG: _side_counts(rag_rows), RUNBOOK: _side_counts(rb_rows)},
        "agentic_rag": {
            "answerable_sampled": len(every),
            "mapped": sum(q.golden_status == "mapped" for q in every),
            "excluded_listed": excluded["listed"],
            "excluded_removed": excluded["removed"],
            "cuad_contracts": {
                "total": len(contracts["calib"] | contracts["test"]),
                "calib": len(contracts["calib"]),
                "test": len(contracts["test"]),
                "both_sides": len(contracts["calib"] & contracts["test"]),
            },
        },
        "runbook_agent": {
            "tasks": len(rb_queries),
            "distinct_tuples": len(tuples),
            "excluded_tuples": len(run_runbook_agent.excluded_tuples(cfg)),
        },
    }
    out[f"{plan_name}.queries.lock.json"] = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    return out


def _comparable(name: str, text: str) -> str:
    if not name.endswith(".lock.json"):
        return text
    doc = cast("dict[str, Any]", json.loads(text))
    return json.dumps({k: v for k, v in doc.items() if k not in UNCOMPARED}, sort_keys=True)


def compare(files: dict[str, str]) -> tuple[int, list[str]]:
    """Regenerated files against the committed ones: how many queries were compared, and the
    names of the files that differ (a lock without its UNCOMPARED keys)."""
    compared = 0
    different: list[str] = []
    for name, text in files.items():
        committed_text = (PLANS / name).read_text(encoding="utf-8")
        if _comparable(name, committed_text) != _comparable(name, text):
            different.append(name)
        if name.endswith(".jsonl"):
            compared += len(querylist.loads(text))
    return compared, different


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    parser.add_argument("command", choices=["generate", "check"])
    parser.add_argument("--plan", default="d1")
    parser.add_argument("--app-dir", type=Path, required=True, help="the agentic-rag checkout")
    args = parser.parse_args()
    app_dir: Path = args.app_dir.resolve()
    uv = os.environ.get("UV") or pinned_uv()
    require_clean(app_dir)
    if args.command == "generate":
        require_clean(ROOT)  # the generator commit the lock records must hold the generator
        files = compute(args.plan, app_dir, uv, committed=True)
        for name, text in files.items():
            (PLANS / name).write_text(text, encoding="utf-8")
        lock = json.loads(files[f"{args.plan}.queries.lock.json"])
        log(f"wrote {sorted(files)}: {json.dumps(lock['counts'])}")
        return 0
    files = compute(args.plan, app_dir, uv, committed=False)
    compared, different = compare(files)
    if different or not compared:
        raise SystemExit(
            f"d1-queries: {compared} queries compared; differs from the committed files:"
            f" {different or 'nothing regenerated'}"
        )
    log(f"{compared} queries in {len(files)} files regenerate byte-identically")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
