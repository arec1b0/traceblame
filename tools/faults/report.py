"""The report of a fault run over both apps: evals/reports/faults-kind-<date>.{json,md} for the
#14 validation run, faults-<plan>-kind-<date>.{json,md} for a run of a committed plan (#70).

Reads .tools/faults/<run>/<app>/{state,check}.json written by run_agentic_rag.py and
run_runbook_agent.py; adds the step_memory_loss gate, the per-component D1 counts and the
throughput projection for D1 (#19), from the measured wall time per kind. For a resumable run
it adds what the resume left (attempts, kills, abandoned traces, duplicates, orphans) and, for
a plan with a split, the distinct queries and traces on each side.

What it reads of a trace comes from the ledger and from check.json's per-trace rows, which are
derived from the ledger: a trace that was abandoned (never in the ledger) is in none of the
tables, only in the resume section.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import checks  # noqa: E402
import faultslib  # noqa: E402
import provenance  # noqa: E402

from traceblame_faults import kinds, ledger, plan, planfile  # noqa: E402

ROOT = faultslib.ROOT
REPORTS = ROOT / "evals" / "reports"
PRELOAD = ROOT / "packages" / "faults" / "preload" / "faults-preload.mjs"
APPS = ("agentic-rag", "runbook-agent")
D1_PLAN = "d1"
DUE = "2026-11-22"


def d1() -> dict[tuple[str, str], int]:
    """D1 as #19 composes it: (app, kind) -> traces; "clean" is the clean share. Read from the
    committed plan file that collects it (tools/faults/plans/d1.yaml), not from a copy here."""
    return planfile.composition(faultslib.config(D1_PLAN))


NOT_RECORDED = "not recorded (predates #65)"


def loop_gate(check: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    k = check["per_kind"].get("step_memory_loss", {})
    rate = k["moved"] / k["applied"] if k.get("applied") else 0.0
    return {
        "applied": k.get("applied", 0),
        "loop_signal": k.get("moved", 0),
        "hit_rate": round(rate, 3),
        "hit_rate_min": float(cfg["loop"]["hit_rate_min"]),
        "repeat_min": int(cfg["loop"]["repeat_min"]),
        "passed": rate >= float(cfg["loop"]["hit_rate_min"]),
    }


def ledger_view(d: Path) -> dict[str, Any]:
    """What the report takes from the run's ledger (the records `record` wrote to ClickHouse)
    and its plan, not from check.json: each trace's planned mode and host load, and the
    applied / not_reached / clean counts per kind, re-derived."""
    path = d / "ledger.jsonl"
    records = ledger.read(path)
    planned = {
        (e.query_ref, e.kind): e for e in plan.loads((d / "plan.jsonl").read_text(encoding="utf-8"))
    }
    modes: dict[str, str | None] = {}
    for r in records:
        e = planned.get((r.query_ref, r.planned_fault_kind))
        mode = r.params.get("mode") if r.injection_id else None
        modes[r.trace_id] = mode or (e.params.get("mode") if e is not None else None)
    counts: dict[str, dict[str, int]] = {}
    for r in records:
        kind = r.planned_fault_kind or "clean"
        counts.setdefault(kind, dict.fromkeys(("clean", "applied", "not_reached"), 0))
        counts[kind][ledger.status(r)] += 1
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "records": len(records),
        "modes": modes,
        "host_load1": {r.trace_id: r.host_load1 for r in records},
        "counts": counts,
    }


def ledger_problems(check: dict[str, Any], view: dict[str, Any]) -> list[str]:
    """The check's per-kind counts against the ones re-derived from the ledger."""
    out: list[str] = []
    for kind, k in check["per_kind"].items():
        got = view["counts"].get(kind, {})
        if (k["applied"], k["not_reached"]) != (got.get("applied", 0), got.get("not_reached", 0)):
            out.append(f"{kind}: check.json and the ledger disagree on applied / not_reached")
    return out


def retrieval_rows(check: dict[str, Any], modes: dict[str, str | None]) -> list[dict[str, Any]]:
    """golden_context_removed traces: the mode that ran (from the ledger, else the plan) and
    whether D1 counts them (checks.retrieval_counted). A planned one that was not reached is
    never counted."""
    rows: list[dict[str, Any]] = []
    for t in check["per_trace"]:
        if t["kind"] != "golden_context_removed":
            continue
        sig = t.get("signal", {})
        clean_hit, faulted_hit = sig.get("golden_in_topk", [None, t.get("golden_in_topk")])
        mode = modes.get(t["trace_id"])
        rows.append(
            {
                "query": t["query"],
                "status": t["status"],
                "mode": mode,
                "golden_in_topk": [clean_hit, faulted_hit],
                "max_score": sig.get("max_score", [None, t.get("max_score")]),
                "moved": sig.get("moved"),
                "counted": t["status"] == "applied" and checks.retrieval_counted(mode, clean_hit),
            }
        )
    return rows


def components() -> dict[str, int]:
    out: dict[str, int] = {}
    for (_, kind), n in d1().items():
        comp = kinds.KINDS[kind].component if kind in kinds.KINDS else "clean"
        out[comp] = out.get(comp, 0) + n
    return out


def projection(checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total = 0.0
    for (app, kind), n in d1().items():
        per = checks.get(app, {}).get("per_kind", {}).get(kind, {})
        seconds = per.get("seconds_median")
        hours = n * float(seconds) / 3600 if seconds is not None else None
        total += hours or 0.0
        rows.append(
            {"app": app, "kind": kind, "traces": n, "seconds_median": seconds, "hours": hours}
        )
    return {
        "rows": rows,
        "hours": round(total, 1),
        "complete": all(r["hours"] is not None for r in rows),
    }


NEEDLES = {
    "agentic-rag": "{queries} sampled CUAD questions: each question's text and every 48-character"
    " window (step 16) of every chunk of its contract ({contract_chunks} chunks, {windows}"
    " windows)",
    "runbook-agent": "{alert} alert needles (32-character windows of each alert summary and the"
    " task-unique label values) and {prompt_windows} 40-character windows of the triage prompt"
    " and its variants",
}


def content_line(app: str, cc: dict[str, Any]) -> str:
    if "needles" not in cc or "scanned" not in cc:  # a check.json from before the full scan
        return f"content check (spans only): {cc}"
    sc = cc["scanned"]
    rows = ", ".join(f"{t} {n}" for t, n in sorted(sc["rows"].items()))
    files = ", ".join(f"{f} ({n} line{'' if n == 1 else 's'})" for f, n in sc["files"].items())
    ex = sc.get("id_fields_excluded")
    excluded = (
        f"; left out: {ex['values']} values of {', '.join(ex['fields'])} that are exactly one of"
        " the run's own dataset ids in its format (`<task>@s<seed>`, CUAD `<title>__<category>`;"
        " a runbook task id is also its workload's name, which the alert needles hold). Any"
        " other value in those fields is scanned"
        if ex
        else ""
    )
    return (
        f"content check: {cc['leaking']} leaking. Needles: "
        + NEEDLES[app].format(**cc["needles"])
        + f". Scanned: every stored row of the run's traces in every table with a trace_id,"
        f" {sc['rows_total']} rows ({rows}); the {sc['exported_spans']} exported spans; the"
        f" run's files {files}{excluded}"
    )


def retrieval_answer(rows: list[dict[str, Any]]) -> list[str]:
    applied = [r for r in rows if r["status"] == "applied"]
    not_reached = [r for r in rows if r["status"] == "not_reached"]
    modes = Counter(str(r["mode"]) for r in applied)
    no_golden = [r for r in applied if r["golden_in_topk"][0] is not True]
    ran = ", ".join(f"`{r['query']}` ran `{r['mode']}`" for r in no_golden) or "none"
    return [
        f"Re-derived from the ledger: {len(applied)} applied "
        f"({', '.join(f'{m} {n}' for m, n in sorted(modes.items()))}), {len(not_reached)} "
        f"not_reached; {sum(r['counted'] for r in applied)} counted for D1.",
        "",
        f"Queries with no golden chunk in the clean top-k: {len(no_golden)}; {ran}.",
    ]


def preload_lines(pre: dict[str, Any] | None) -> list[str]:
    if pre is None:
        return []
    out = [
        f"- preload used by this run: sha256 `{pre['run_sha256']}` "
        + (
            "(equal to the file at the run's commit)"
            if pre["run_commit_matches"]
            else "(**not** the file at the run's commit)"
        )
    ]
    if pre["committed_sha256"] == pre["run_sha256"]:
        out.append("- the committed preload is the same file")
        return out
    added, removed = pre["lines_added_removed"]
    since = "; ".join(f"`{c}`" for c in pre["commits_since"]) or "none"
    out.append(
        f"- the committed preload differs: sha256 `{pre['committed_sha256']}`, changed since the"
        f" run by {since} ({added} lines added, {removed} removed). D1 runs with the committed"
        " preload: the build phase refuses a dirty checkout and records the file's sha256 in the"
        " run's provenance (`preload_sha256`)"
    )
    return out


def loads_by_kind(check: dict[str, Any], view: dict[str, Any]) -> dict[str, float | None]:
    """The median host load per kind, with check_run's helper (faultslib.median)."""
    out: dict[str, list[float]] = {}
    for t in check["per_trace"]:
        load = view["host_load1"].get(t["trace_id"])
        if load is not None:
            out.setdefault(t["kind"], []).append(float(load))
    return {k: faultslib.median(v) for k, v in out.items()}


def dirty_problems(s: dict[str, Any]) -> list[str]:
    """A run built from a dirty checkout (the drivers refuse one; this catches an edited state)."""
    out = [
        f"the {what} checkout was dirty"
        for what, key in (("traceblame", "dirty"), ("app", "app_dirty"))
        if s.get(key) is True
    ]
    return out


def provenance_info(s: dict[str, Any]) -> dict[str, Any]:
    """The #65 fields of one app's state, or NOT_RECORDED for a run that predates them."""
    return {
        "dirty": s.get("dirty", NOT_RECORDED),
        "app_dirty": s.get("app_dirty", NOT_RECORDED),
        "phases": s.get("provenance") or NOT_RECORDED,
    }


def _flag(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _schema_cell(snap: dict[str, Any]) -> str:
    cells: list[str] = []
    for db, label in (("clickhouse", "ClickHouse"), ("postgres", "Postgres")):
        rows = snap["migrations"][db]
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:8]
        cells.append(f"{label} {len(rows)} (`{digest}`)")
    return ", ".join(cells)


def _image_table(snap: dict[str, Any]) -> list[str]:
    out = ["  | Role | Pods | Container | imageID |", "  |---|---|---|---|"]
    for role, containers in snap["images"].items():
        groups: dict[tuple[str, str], int] = {}
        for c in containers:
            groups[(c["container"], c["image_id"])] = (
                groups.get((c["container"], c["image_id"]), 0) + 1
            )
        for (container, image_id), n in sorted(groups.items()):
            out.append(f"  | {role} | {n} | {container} | `{image_id or '-'}` |")
        if not containers:
            out.append(f"  | {role} | 0 | - | - |")
    return out


def provenance_lines(app: str, s: dict[str, Any], info: dict[str, Any]) -> list[str]:
    """Each app's provenance section: the dirty flag, what kind ran (pod imageID) and the
    migrations both databases had applied, per phase, and whether they changed during it."""
    dirty = (
        f"traceblame {_flag(info['dirty'])}, app {_flag(info['app_dirty'])}"
        if isinstance(info["dirty"], bool)
        else f"{NOT_RECORDED}; the build refused a dirty checkout (`require_clean`)"
    )
    out = [f"- dirty flag at the build: {dirty}"]
    phases = info["phases"]
    built = s.get("build", {}).get("eval_image_id")
    if not isinstance(phases, dict):
        out += [
            f"- applied migrations (ClickHouse and Postgres `schema_migrations`): {NOT_RECORDED}",
            f"- pipeline image IDs (otel agent and gateway, trace-assembler, ClickHouse, NATS):"
            f" {NOT_RECORDED}",
        ]
        if app == "runbook-agent":
            out.append(f"- eval pod image ID: {NOT_RECORDED}; built eval_image_id `{built}`")
        return out
    phases = cast("dict[str, dict[str, Any]]", phases)
    first = phases.get("run") or next(iter(phases.values()), None)
    out += [
        "- applied migrations and the images kind ran, read at the start and the end of each"
        " phase (a change within a phase, or a read that found nothing, is a problem):",
        "",
        "  | Phase | Start | End | Applied migrations (count, list hash) at the end | Changed"
        " within the phase | Problems |",
        "  |---|---|---|---|---|---|",
    ]
    for name, rec in phases.items():
        changed = "; ".join(provenance.changes(rec["start"], rec["end"])) or "none"
        out.append(
            f"  | {name} | {rec['start']['at']} | {rec['end']['at']} | {_schema_cell(rec['end'])}"
            f" | {changed} | {len(provenance.phase_problems(rec))} |"
        )
    if first is not None:
        mig = first["end"]["migrations"]
        out += [
            "",
            f"  ClickHouse at the end of the {'run' if first is phases.get('run') else 'first'}"
            f" phase: {_migrations_text(mig['clickhouse'])}",
            "",
            f"  Postgres: {_migrations_text(mig['postgres'])}",
            "",
            *_image_table(first["end"]),
        ]
    eval_rec = (phases.get("run") or {}).get("eval")
    if eval_rec is not None:
        pods = [c for sample in phases["run"]["eval_pods"] for c in sample["pods"]]
        ids = sorted({c["image_id"] for c in pods})
        node_ids = sorted({i for c in pods for i in c.get("node_config_ids", [])})
        built_cfg = eval_rec.get("built_config_id")
        verdict = (
            "equal"
            if built_cfg and node_ids == [built_cfg] and all(c.get("node_config_ids") for c in pods)
            else "**not equal**"
        )
        out += [
            "",
            f"  Eval image of the run: `{eval_rec['image']}`. Proof: the config digest built"
            f" `{built_cfg or 'not recorded'}` against the config digest the pods' nodes held for"
            f" their image IDs (`crictl images`, matched on the pod's imageID)"
            f" {', '.join(f'`{i}`' for i in node_ids) or 'none'}: {verdict}. Additional signals:"
            f" the pods ran the built tag and kept the imageID"
            + (" ".join(f" `{i}`" for i in ids) or " none")
            + f" across {len(phases['run']['eval_pods'])} samples (one per deploy and one at the"
            f" end). For reference, the build's `eval_image_id` is"
            f" `{eval_rec['built_image_id']}`, an index digest that a node never reports"
            " (ADR-0009).",
        ]
    return out


def _migrations_text(rows: list[dict[str, str]]) -> str:
    return (
        "; ".join(
            f"{m['version']} {m['name']} ({m['phase']}, {m['sha256']}, {m['applied_at']})"
            for m in rows
        )
        or "none"
    )


def split_view(d: Path, records: list[ledger.Record]) -> dict[str, Any] | None:
    """Distinct queries per split side, and traces per kind and side, of one app: the sides
    from the run's queries.jsonl (the plan's committed list, verified at prepare/build), the
    traces from the ledger. None for a plan without a split."""
    queries = plan.load_queries((d / "queries.jsonl").read_text(encoding="utf-8"))
    sides = {q.ref: q.split for q in queries}
    if not any(sides.values()):
        return None
    traces: dict[str, dict[str, int]] = {}
    for r in records:
        kind = r.planned_fault_kind or "clean"
        traces.setdefault(kind, {"calib": 0, "test": 0})[sides[r.query_ref]] += 1
    planned = Counter(sides.values())
    ran = Counter(sides[ref] for ref in {r.query_ref for r in records})
    return {
        "queries_planned": {side: planned.get(side, 0) for side in ("calib", "test")},
        "queries_with_traces": {side: ran.get(side, 0) for side in ("calib", "test")},
        "traces": dict(sorted(traces.items())),
    }


def split_lock(plan_name: str) -> dict[str, Any] | None:
    """The lock of the plan's committed query list (what it was generated from, its counts)."""
    path = faultslib.PLANS / f"{plan_name}.queries.lock.json"
    if not path.is_file():
        return None
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def resume_problems(check: dict[str, Any]) -> list[str]:
    """The resume check's problems, re-derived from its own counts so that a check that checked
    nothing cannot pass: zero entries, stored traces or eval_traces rows; an orphan; an
    injection id with rows on more than one trace."""
    resume = check.get("resume")
    if resume is None:
        return []
    out = [f"resume: {p}" for p in resume["problems"]]
    for key in ("entries", "stored_traces", "eval_traces_rows"):
        if not resume.get(key):
            out.append(f"resume: nothing checked ({key} = {resume.get(key)})")
    if resume.get("orphans"):
        out.append(f"resume: {len(resume['orphans'])} orphan traces")
    if resume.get("duplicate_injections"):
        out.append(
            f"resume: {resume['duplicate_injections']} injection ids with injections rows on"
            " more than one trace"
        )
    return out


def resume_lines(resume: dict[str, Any] | None) -> list[str]:
    """One app's resume section: what was checked, every attempt, every abandoned trace."""
    if resume is None:
        return []
    gone = resume["abandoned"]
    by_kind = ", ".join(f"{k} {n}" for k, n in sorted(resume["abandoned_by_kind"].items()))
    by_reason = ", ".join(f"{k} {n}" for k, n in sorted(resume["abandoned_by_reason"].items()))
    out = [
        "",
        "Resume (#70):",
        "",
        f"- checked: {resume['entries']} plan entries, {resume['records']} ledger records,"
        f" {resume['eval_traces_rows']} `eval_traces` rows, {resume['stored_traces']} traces"
        f" stored under the run's marker, {resume['injection_ids_checked']} injection ids",
        f"- attempts: {len(resume['attempts'])}, killed: {resume['killed']}; entries in flight"
        f" that ran again: {resume['lost_entries']}; torn ledger tails: {resume['torn_tails']}",
        f"- abandoned traces (in `eval_exclusions`, never in `eval_traces` or `injections`):"
        f" {len(gone)}" + (f"; by kind: {by_kind}; by reason: {by_reason}" if gone else ""),
        f"- injection ids with `injections` rows on more than one trace:"
        f" {resume['duplicate_injections']}",
        f"- orphans (stored, neither completed nor abandoned): {len(resume['orphans'])}"
        + (
            f"; found by the orphan sweep and abandoned: {resume['swept']}"
            if resume["swept"]
            else ""
        ),
        "",
        "| Attempt | Status | Started | Ended | Last event |",
        "|---|---|---|---|---|",
    ]
    out += [
        f"| {a['id']} | {a['status']} | {a['started_at']} | {a['ended_at'] or '-'} |"
        f" {a['last_event']} |"
        for a in resume["attempts"]
    ]
    if gone:
        out += [
            "",
            "| Abandoned trace | Entry | Kind | Attempt | Reason | Found in | Stored spans |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| `{a['trace_id']}` | {a['entry_id']} | {a['kind']} | {a['attempt_id']} |"
            f" {a['reason']} | {a['source']} | {a['stored_spans']} |"
            for a in gone
        ]
    return out


def split_lines(report: dict[str, Any]) -> list[str]:
    """The split section: distinct queries per app and side, traces per app, kind and side."""
    splits: dict[str, dict[str, Any]] = report.get("split") or {}
    if not splits:
        return []
    out = [
        "## Calibration / test split",
        "",
        "By source query, 50/50 per app, every fault kind within 2 queries of half on the test",
        f"side (`tools/faults/plans/{report['plan']}.queries.jsonl`, committed before the run).",
        "The grouping key is the CUAD question id for agentic-rag and the task's (scenario,",
        "parameters) tuple for runbook-agent. #19 reports query-cluster bootstrap 95% CIs and",
        "flags cells with n < 30 as indicative.",
        "",
        "| App | Queries calib | Queries test | With traces calib | With traces test |",
        "|---|---|---|---|---|",
    ]
    for app, view in splits.items():
        p, r = view["queries_planned"], view["queries_with_traces"]
        out.append(f"| {app} | {p['calib']} | {p['test']} | {r['calib']} | {r['test']} |")
    out += ["", "| App | Kind | Traces calib | Traces test |", "|---|---|---|---|"]
    for app, view in splits.items():
        out += [
            f"| {app} | {kind} | {n['calib']} | {n['test']} |" for kind, n in view["traces"].items()
        ]
    lock = report.get("split_lock")
    if lock is not None:
        c = lock["agentic_rag"]["cuad_contracts"]
        rb = lock["runbook_agent"]
        out += [
            "",
            f"CUAD contracts are not grouped: the {lock['counts']['agentic-rag']['queries']}"
            f" questions come from {c['total']} contracts, {c['calib']} with a question on the"
            f" calibration side, {c['test']} on the test side, and **{c['both_sides']} on both**.",
            "",
            f"runbook-agent: {rb['tasks']} tasks with {rb['distinct_tuples']} distinct (scenario,"
            f" parameters) tuples; {rb['excluded_tuples']} validation tuples excluded.",
            f" agentic-rag: {lock['agentic_rag']['excluded_listed']} validation candidates"
            f" listed, {lock['agentic_rag']['excluded_removed']} of them removed from this"
            " plan's sample.",
        ]
    return [*out, ""]


def title(report: dict[str, Any]) -> str:
    plan_name = report.get("plan", faultslib.VALIDATION)
    what = "validation run" if plan_name == faultslib.VALIDATION else f"`{plan_name}` run"
    return f"# Fault injection through kind: {what} ({report['date']})"


def stem(run: dict[str, Any]) -> str:
    """The report's file name: faults-kind-<date> for the validation run, as before;
    faults-<plan>-kind-<date> for a committed plan, with the run id's -<n> when the day and the
    commit had more than one run."""
    plan_name = faultslib.run_plan(run)
    if plan_name == faultslib.VALIDATION:
        return f"faults-kind-{run_date(run)}"
    again = re.search(r"-s\d+-[0-9a-f]{7}-(\d+)$", str(run["run_id"]))
    return f"faults-{plan_name}-kind-{run_date(run)}" + (f"-{again.group(1)}" if again else "")


def markdown(report: dict[str, Any]) -> str:
    out = [
        title(report),
        "",
        "`tools/faults/` (#14): the six fault kinds injected into real runs of agentic-rag",
        "(CUAD) and runbook-agent (sandbox alerts), exported through kind as `tenant-eval`.",
        "Per kind: the marker survives on the intended span, the injections row joins it, the",
        "signal the #16 detector reads moved against a clean trace, and the trace reaches",
        "`trace.completed`. Machine-readable: the `.json` next to this file.",
        "",
    ]
    for app in APPS:
        c = report["apps"].get(app)
        if c is None:
            out += [f"## {app}", "", "Not run.", ""]
            continue
        s = report["state"][app]
        view = report["ledger"][app]
        load = loads_by_kind(c, view)
        out += [
            f"## {app}",
            "",
            f"- run `{c['run_id']}`, traceblame `{s['traceblame_commit'][:12]}`, "
            f"app `{s['app_commit'][:12]}`",
            f"- plan sha256 `{s['plan']['sha256'][:16]}`, {s['plan']['entries']} entries, seed "
            f"{s['seed']}; ledger sha256 `{view['sha256'][:16]}`, {view['records']} records "
            "(the counts below are re-derived from it)",
            f"- spans sent {c['spans']['sent']}, stored {c['spans']['stored']}; baseline digest "
            f"`{(c.get('baseline_digest') or '-')[:23]}`",
            f"- {content_line(app, c['content_check'])}",
            "- host 1-minute load average per trace: "
            + (
                f"in the tables below ({s.get('host_cpus') or '?'} CPUs)"
                if load
                else "not recorded in this run (it predates the field; the drivers record it"
                " in the ledger from now on, and this run is not repeated for it)"
            ),
        ]
        out += provenance_lines(app, s, report["provenance"][app])
        if app == "runbook-agent":
            out.append(f"- image guard {s['image_guard']}")
            out += preload_lines(report.get("preload"))
        if app == "agentic-rag":
            out.append(f"- golden chunks of the sampled answerable queries: {s['golden_coverage']}")
        out += resume_lines(c.get("resume"))
        out += [
            "",
            "| Kind | Component | Traces | Applied | Not reached | Marker on its span | Joined |"
            " Signal moved | Assembled | Median s | p95 s | Load1 median |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for kind, k in c["per_kind"].items():
            if not k["traces"]:
                continue
            got = view["counts"].get(kind, {})
            out.append(
                f"| {kind} | {k['component'] or '-'} | {k['traces']} | "
                f"{got.get('applied', 0)} | {got.get('not_reached', 0)} | {k['marker_ok']} | "
                f"{k['joined']} | {k['moved']} | {k['assembled']} | {k['seconds_median']} | "
                f"{k['seconds_p95']} | {load.get(kind, '-')} |"
            )
        out += ["", "Per trace (query = sha256 of the dataset id, first 12):", ""]
        out += [
            "| Query | Kind | Status | Outcome | Load1 | Signal |",
            "|---|---|---|---|---|---|",
        ]
        for t in c["per_trace"]:
            sig = {k: v for k, v in t.get("signal", {}).items() if k != "moved"}
            moved = t.get("signal", {}).get("moved")
            mark = "" if moved is None else ("moved " if moved else "NOT moved ")
            t_load = view["host_load1"].get(t["trace_id"])
            out.append(
                f"| {t['query']} | {t['kind']} | {t['status']} | {t['outcome']} | "
                f"{'-' if t_load is None else t_load} | {mark}{sig} |"
            )
        out.append("")
    retrieval = report["retrieval"]
    if retrieval:
        out += [
            "## golden_context_removed: mode and the D1 rule",
            "",
            "D1 (#19) counts a `golden_context_removed` trace as a retrieval fault only when",
            "the golden set was in the clean run's top-k, or the mode is `distractor`. A",
            "`remove` whose run never had a golden chunk in its results is `not_reached`: the",
            "store wrapper reports nothing removed, the ledger gets no injection id, and",
            "`injections` gets no row.",
            "",
            "| Query | Mode | Golden in clean top-k | Golden in faulted top-k | Max score clean -> "
            "faulted | Signal moved | Counted |",
            "|---|---|---|---|---|---|---|",
        ]
        out[-2:-2] = [*retrieval_answer(retrieval), ""]
        for r in retrieval:
            out.append(
                f"| {r['query']} | {r['mode']} | {r['golden_in_topk'][0]} | "
                f"{r['golden_in_topk'][1]} | {r['max_score'][0]} -> {r['max_score'][1]} | "
                f"{r['moved']} | {r['counted']} |"
            )
        out.append("")
    out += split_lines(report)
    gate = report["loop_gate"]
    out += [
        "## step_memory_loss gate",
        "",
        f"The loop signal (one `(tool_name, args_hash)` at least {gate['repeat_min']} times) "
        f"fired on {gate['loop_signal']} of {gate['applied']} applied injections: "
        f"{gate['hit_rate']:.0%}, threshold {gate['hit_rate_min']:.0%} "
        "(`tools/faults/validation.yaml`). "
        + (
            (
                "**Passed**: the kind is collected for D1."
                if gate["passed"]
                else "**Failed**: the kind is redesigned before D1, not collected as it is."
            )
            if gate.get("gated", True)
            else "Reported, not gated: the gate was the #14 validation run's."
        ),
        "",
        "## Reproducibility",
        "",
        "Fixed by the seed: the plan (which query gets which kind, its parameters and injection",
        "point; byte-identical, its sha256 above), the injection ids, and a `seed` on every",
        "Ollama chat request (`ids.request_seed(seed, query_ref, call_index)`), clean and faulted",
        "alike. The apps' own temperatures are unchanged (agentic-rag: agent 0.2, judge 0.0,",
        "summarizer 0.3; runbook-agent as recorded per request below).",
        "",
        "Not reproducible: token-level model output across Ollama versions, hardware, threads",
        "and parallel requests (the request seed is best-effort); therefore the agent's path,",
        "and whether a conditional injection point is reached (`not_reached`); judge scores;",
        "latencies; trace and span ids; sandbox alert timing.",
        "",
    ]
    rb = report["apps"].get("runbook-agent")
    if rb is not None:
        out += [
            f"runbook-agent request fields seen by the preload: {report['runbook_requests']}",
            "",
        ]
    proj = report["projection"]
    comp = report["d1_components"]
    out += [
        "## D1 projection (#19)",
        "",
        "Wall time per trace measured here (median per kind) times the D1 composition proposed",
        "on #19. Orchestration has two kinds, so D1 holds "
        f"{comp.get('orchestration', 0)} orchestration traces",
        f"against {comp.get('retrieval', 0)} for each other component; #19 reports macro-F1 by",
        "component and per app as well as pooled.",
        "",
        "| App | Kind | Traces | Median s | Hours |",
        "|---|---|---|---|---|",
    ]
    for r in proj["rows"]:
        hours = "-" if r["hours"] is None else f"{r['hours']:.1f}"
        out.append(
            f"| {r['app']} | {r['kind']} | {r['traces']} | {r['seconds_median']} | {hours} |"
        )
    out += [
        "",
        f"Total about {proj['hours']} h of serial run time (Ollama serves one request at a time),"
        f" plus settle and assembly; M2 is due {DUE}.",
        "",
        *(
            ["## Notes", "", *[f"- {n}" for n in report.get("notes", [])], ""]
            if report.get("notes")
            else []
        ),
        "## Blockers",
        "",
        *([f"- {b}" for b in report["blockers"]] or ["None recorded."]),
        "",
        "## Result",
        "",
        "All checks passed."
        if not report["problems"]
        else "Problems:\n\n" + "\n".join(f"- {p}" for p in report["problems"]),
        "",
    ]
    return "\n".join(out)


def _git(*argv: str) -> bytes:
    return subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(ROOT), *argv],  # noqa: S607 - git from PATH, as the drivers use it
        capture_output=True,
        check=True,
    ).stdout


def preload_provenance(run_commit: str, run_sha256: str) -> dict[str, Any]:
    """The preload the run used against the committed one, from git: the file at the run's
    commit must have the run's sha256; what changed since is listed by commit and lines."""
    rel = str(PRELOAD.relative_to(ROOT))
    at_run = hashlib.sha256(_git("show", f"{run_commit}:{rel}")).hexdigest()
    committed = hashlib.sha256(_git("show", f"HEAD:{rel}")).hexdigest()
    numstat = _git("diff", "--numstat", run_commit, "HEAD", "--", rel).decode().split()
    return {
        "run_sha256": run_sha256,
        "run_commit_matches": at_run == run_sha256,
        "committed_sha256": committed,
        "commits_since": _git("log", "--format=%h %s", f"{run_commit}..HEAD", "--", rel)
        .decode()
        .splitlines(),
        "lines_added_removed": [int(numstat[0]), int(numstat[1])] if numstat else [0, 0],
    }


def run_date(run: dict[str, Any]) -> str:
    """The run's own date, from its run.json, so a re-generated report keeps its file."""
    return str(run["created_at"])[:10]


def signal_problems(check: dict[str, Any]) -> list[str]:
    """Applied injections whose signal did not move (checks.signal_problem), re-derived from the
    per-trace results, so a check.json written before check_run reported them cannot pass."""
    out: list[str] = []
    for t in check["per_trace"]:
        if t["status"] != "applied":
            continue
        problem = checks.signal_problem(t["kind"], t.get("signal", {}), t["trace_id"])
        if problem is not None and problem not in check["problems"]:
            out.append(problem)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--blocker", action="append", default=[])
    parser.add_argument("--note", action="append", default=[])
    args = parser.parse_args()
    cfg = faultslib.run_config(args.run_id)
    run = faultslib.open_run(args.run_id)
    plan_name = faultslib.run_plan(run)
    per_app: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    views: dict[str, dict[str, Any]] = {}
    for app in APPS:
        d = faultslib.run_dir(args.run_id, app)
        if (d / "check.json").is_file():
            per_app[app] = json.loads((d / "check.json").read_text(encoding="utf-8"))
            states[app] = faultslib.load_state(d)
            views[app] = ledger_view(d)
    if not per_app:
        raise SystemExit(f"report: no check.json under .tools/faults/{args.run_id}/")
    gate = loop_gate(per_app.get("agentic-rag", {"per_kind": {}}), cfg)
    # The gate decided whether D1 collects the kind: it is the validation run's.
    gate["gated"] = plan_name == faultslib.VALIDATION
    problems = [f"{app}: {p}" for app, c in per_app.items() for p in c["problems"]]
    problems += [
        f"{app}: {p}"
        for app, c in per_app.items()
        for p in resume_problems(c)
        if p not in c["problems"]
    ]
    problems += [
        f"{app}: {p}" for app, c in per_app.items() for p in ledger_problems(c, views[app])
    ]
    problems += [f"{app}: {p}" for app, c in per_app.items() for p in signal_problems(c)]
    problems += [
        f"{app}: {p}"
        for app, c in per_app.items()
        for p in faultslib.content_problems(c["content_check"])
        if p not in c["problems"]
    ]
    problems += [
        f"{app}: {p}"
        for app, s in states.items()
        for p in dirty_problems(s) + provenance.run_problems(s.get("provenance"), app)
    ]
    if gate["gated"] and not gate["passed"]:
        problems.append("step_memory_loss: loop signal below the gate; redesign before D1")
    splits = {
        app: view
        for app in per_app
        if (
            view := split_view(
                faultslib.run_dir(args.run_id, app),
                ledger.read(faultslib.run_dir(args.run_id, app) / "ledger.jsonl"),
            )
        )
        is not None
    }
    requests: list[Any] = []
    rb_dir = faultslib.run_dir(args.run_id, "runbook-agent")
    if rb_dir.is_dir():
        for outcome in faultslib.read_outcomes(rb_dir):
            for req in outcome.get("request", []):
                fields = cast("dict[str, Any]", req or {})
                requests.append({k: v for k, v in fields.items() if k != "seed"})
    rag_view = views.get("agentic-rag", {"modes": {}})
    report = {
        "date": run_date(run),
        "run_id": args.run_id,
        "plan": plan_name,
        "run": run,
        "split": splits,
        "split_lock": split_lock(plan_name) if splits else None,
        "config": cfg,
        "apps": per_app,
        "state": {
            app: {
                k: v
                for k, v in s.items()
                if k not in ("app_dir", "archive", "embedding", "ollama", "provenance")
            }
            for app, s in states.items()
        },
        "provenance": {app: provenance_info(s) for app, s in states.items()},
        "ledger": {
            app: {k: v for k, v in view.items() if k != "modes"} for app, view in views.items()
        },
        "loop_gate": gate,
        "retrieval": retrieval_rows(
            per_app.get("agentic-rag", {"per_trace": []}), rag_view["modes"]
        ),
        "d1_components": components(),
        "projection": projection(per_app),
        "runbook_requests": sorted({json.dumps(r, sort_keys=True) for r in requests}),
        "blockers": args.blocker,
        "problems": problems,
        "preload": preload_provenance(
            states["runbook-agent"]["traceblame_commit"], states["runbook-agent"]["preload_sha256"]
        )
        if "runbook-agent" in states
        else None,
        "notes": args.note,
    }
    name = stem(run)
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{name}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (REPORTS / f"{name}.md").write_text(markdown(report), encoding="utf-8")
    print(f"report: evals/reports/{name}.md ({len(problems)} problems)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
