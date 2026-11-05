"""The #14 validation report over both apps: evals/reports/faults-kind-<date>.{json,md}.

Reads .tools/faults/<run>/<app>/{state,check}.json written by run_agentic_rag.py and
run_runbook_agent.py; adds the step_memory_loss gate, the per-component D1 counts and the
throughput projection for D1 (#19), from the measured wall time per kind.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import faultslib  # noqa: E402

from traceblame_faults import kinds  # noqa: E402

ROOT = faultslib.ROOT
REPORTS = ROOT / "evals" / "reports"
APPS = ("agentic-rag", "runbook-agent")
# D1 as proposed on #19: (app, kind) -> traces; "clean" is the clean share.
D1: dict[tuple[str, str], int] = {
    ("agentic-rag", "golden_context_removed"): 75,
    ("agentic-rag", "tool_failure"): 75,
    ("agentic-rag", "output_budget_exhausted"): 75,
    ("agentic-rag", "step_memory_loss"): 75,
    ("agentic-rag", "malformed_output"): 50,
    ("agentic-rag", "prompt_variant"): 50,
    ("agentic-rag", "clean"): 110,
    ("runbook-agent", "malformed_output"): 25,
    ("runbook-agent", "prompt_variant"): 25,
    ("runbook-agent", "clean"): 40,
}
DUE = "2026-11-22"


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


def components() -> dict[str, int]:
    out: dict[str, int] = {}
    for (_, kind), n in D1.items():
        comp = kinds.KINDS[kind].component if kind in kinds.KINDS else "clean"
        out[comp] = out.get(comp, 0) + n
    return out


def projection(checks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total = 0.0
    for (app, kind), n in D1.items():
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


def markdown(report: dict[str, Any]) -> str:
    out = [
        f"# Fault injection through kind: validation run ({report['date']})",
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
        out += [
            f"## {app}",
            "",
            f"- run `{c['run_id']}`, traceblame `{s['traceblame_commit'][:12]}`, "
            f"app `{s['app_commit'][:12]}`",
            f"- plan sha256 `{s['plan']['sha256'][:16]}`, {s['plan']['entries']} entries, seed "
            f"{s['seed']}",
            f"- spans sent {c['spans']['sent']}, stored {c['spans']['stored']}; baseline digest "
            f"`{(c.get('baseline_digest') or '-')[:23]}`",
            f"- content check: {c['content_check']}",
        ]
        if app == "runbook-agent":
            out.append(
                f"- preload sha256 `{s['preload_sha256'][:16]}`; image guard {s['image_guard']}"
            )
        if app == "agentic-rag":
            out.append(f"- golden chunks of the sampled answerable queries: {s['golden_coverage']}")
        out += [
            "",
            "| Kind | Component | Traces | Applied | Not reached | Marker on its span | Joined |"
            " Signal moved | Assembled | Median s | p95 s |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for kind, k in c["per_kind"].items():
            if not k["traces"]:
                continue
            out.append(
                f"| {kind} | {k['component'] or '-'} | {k['traces']} | {k['applied']} | "
                f"{k['not_reached']} | {k['marker_ok']} | {k['joined']} | {k['moved']} | "
                f"{k['assembled']} | {k['seconds_median']} | {k['seconds_p95']} |"
            )
        out += ["", "Per trace (query = sha256 of the dataset id, first 12):", ""]
        out += ["| Query | Kind | Status | Outcome | Signal |", "|---|---|---|---|---|"]
        for t in c["per_trace"]:
            sig = {k: v for k, v in t.get("signal", {}).items() if k != "moved"}
            moved = t.get("signal", {}).get("moved")
            mark = "" if moved is None else ("moved " if moved else "NOT moved ")
            out.append(
                f"| {t['query']} | {t['kind']} | {t['status']} | {t['outcome']} | {mark}{sig} |"
            )
        out.append("")
    gate = report["loop_gate"]
    out += [
        "## step_memory_loss gate",
        "",
        f"The loop signal (one `(tool_name, args_hash)` at least {gate['repeat_min']} times) "
        f"fired on {gate['loop_signal']} of {gate['applied']} applied injections: "
        f"{gate['hit_rate']:.0%}, threshold {gate['hit_rate_min']:.0%} "
        "(`tools/faults/validation.yaml`). "
        + (
            "**Passed**: the kind is collected for D1."
            if gate["passed"]
            else "**Failed**: the kind is redesigned before D1, not collected as it is."
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


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--blocker", action="append", default=[])
    args = parser.parse_args()
    cfg = faultslib.config()
    checks: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    for app in APPS:
        d = faultslib.run_dir(args.run_id, app)
        if (d / "check.json").is_file():
            checks[app] = json.loads((d / "check.json").read_text(encoding="utf-8"))
            states[app] = faultslib.load_state(d)
    if not checks:
        raise SystemExit(f"report: no check.json under .tools/faults/{args.run_id}/")
    gate = loop_gate(checks.get("agentic-rag", {"per_kind": {}}), cfg)
    problems = [f"{app}: {p}" for app, c in checks.items() for p in c["problems"]]
    if not gate["passed"]:
        problems.append("step_memory_loss: loop signal below the gate; redesign before D1")
    requests: list[Any] = []
    rb_dir = faultslib.run_dir(args.run_id, "runbook-agent")
    if (rb_dir / "outcomes.jsonl").is_file():
        for line in (rb_dir / "outcomes.jsonl").read_text(encoding="utf-8").splitlines():
            for req in json.loads(line).get("request", []):
                fields = cast("dict[str, Any]", req or {})
                requests.append({k: v for k, v in fields.items() if k != "seed"})
    report = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "run_id": args.run_id,
        "config": cfg,
        "apps": checks,
        "state": {
            app: {
                k: v for k, v in s.items() if k not in ("app_dir", "archive", "embedding", "ollama")
            }
            for app, s in states.items()
        },
        "loop_gate": gate,
        "d1_components": components(),
        "projection": projection(checks),
        "runbook_requests": sorted({json.dumps(r, sort_keys=True) for r in requests}),
        "blockers": args.blocker,
        "problems": problems,
    }
    stem = f"faults-kind-{report['date']}"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (REPORTS / f"{stem}.md").write_text(markdown(report), encoding="utf-8")
    print(f"report: evals/reports/{stem}.md ({len(problems)} problems)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
