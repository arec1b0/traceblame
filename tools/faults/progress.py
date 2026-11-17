"""Progress of a fault run (#70): read-only. No kind access, no lock, no writes; safe to run
next to a running attempt.

Per app: entries done / remaining / abandoned by kind, every attempt with its status (running
when its pid is alive, otherwise killed or ended), the entry in flight, the age of the last
ledger line, a torn ledger tail if there is one, and an ETA: the remaining entries times this
run's median seconds per kind, falling back to the #14 validation run's medians
(evals/reports/faults-kind-2026-11-05.json) for a kind this run has not measured yet. Also the
median host load of the run's traces.
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import faultslib  # noqa: E402

from traceblame_faults import appendlog, attempts, ledger, plan, runner  # noqa: E402

APPS = ("agentic-rag", "runbook-agent")
FALLBACK = faultslib.ROOT / "evals" / "reports" / "faults-kind-2026-11-05.json"


def fallback_medians(path: Path = FALLBACK) -> dict[str, dict[str, float]]:
    """app -> kind -> median seconds per trace, as the #14 validation run measured them."""
    if not path.is_file():
        return {}
    report = cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    return {
        app: {
            kind: float(k["seconds_median"])
            for kind, k in check["per_kind"].items()
            if k.get("seconds_median") is not None
        }
        for app, check in report["apps"].items()
    }


def _whole_lines(path: Path) -> list[str]:
    """The file's complete lines; a torn tail is left out (and reported separately)."""
    if not path.is_file():
        return []
    data = path.read_bytes()
    return data[: data.rfind(b"\n") + 1].decode("utf-8").splitlines()


def _alive(pid: int) -> bool:
    return runner.proc_start(pid) is not None


def app_progress(d: Path, fallback: dict[str, float], now: float) -> dict[str, Any] | None:
    """One app's progress from its run directory, or None if the app was not prepared."""
    if not (d / "plan.jsonl").is_file():
        return None
    entries = plan.loads((d / "plan.jsonl").read_text(encoding="utf-8"))
    ledger_path = d / runner.LEDGER
    records = [ledger.loads(line) for line in _whole_lines(ledger_path) if line.strip()]
    done = {r.entry_id for r in records if r.entry_id is not None}
    events = [json.loads(line) for line in _whole_lines(d / attempts.FILE) if line.strip()]
    kind_of = {e.entry_id: e.kind or "clean" for e in entries}
    by_kind: dict[str, dict[str, int]] = {}
    for e in entries:
        row = by_kind.setdefault(e.kind or "clean", {"done": 0, "remaining": 0, "abandoned": 0})
        row["done" if e.entry_id in done else "remaining"] += 1
    gone = attempts.abandoned(events)
    for a in gone:
        by_kind.setdefault(
            a.planned_fault_kind or "clean", {"done": 0, "remaining": 0, "abandoned": 0}
        )["abandoned"] += 1

    views: list[dict[str, Any]] = []
    for a in attempts.attempts(events):
        status = a.status
        if status == "open":
            status = "running" if _alive(a.pid) else "killed (not resumed yet)"
        views.append(
            {"id": a.id, "pid": a.pid, "status": status, "started_at": a.started_at,
             "ended_at": a.ended_at, "last_event": a.last_event}
        )  # fmt: skip
    running = [v for v in views if v["status"] == "running"]
    in_flight = [
        s
        for s in attempts.started(events)
        if not s.ended and s.entry_id not in done and running and s.attempt_id == running[-1]["id"]
    ]
    current = None
    if in_flight:
        s = in_flight[-1]
        current = {
            "entry_id": s.entry_id,
            "kind": kind_of.get(s.entry_id, "?"),
            "seconds": round(now - attempts.seconds(s.at), 1),
            "trace_ids": list(s.trace_ids),
        }

    seconds: dict[str, list[float]] = {}
    for path in sorted(d.glob("outcomes.a[0-9]*.jsonl")):
        for line in _whole_lines(path):
            o = json.loads(line)
            if o.get("seconds") is not None and o.get("status") != "no_trace":
                seconds.setdefault(str(o.get("kind") or "clean"), []).append(float(o["seconds"]))
    medians = {k: round(statistics.median(v), 1) for k, v in seconds.items()}
    eta = 0.0
    unknown: list[str] = []
    sources: dict[str, str] = {}
    for kind, row in sorted(by_kind.items()):
        if not row["remaining"]:
            continue
        if kind in medians:
            eta += row["remaining"] * medians[kind]
            sources[kind] = "this run"
        elif kind in fallback:
            eta += row["remaining"] * fallback[kind]
            sources[kind] = "validation run"
        else:
            unknown.append(kind)
    loads = [float(r.host_load1) for r in records if r.host_load1 is not None]
    torn = appendlog.tail(ledger_path)
    return {
        "entries": len(entries),
        "done": len(done),
        "remaining": len(entries) - len(done),
        "abandoned": len(gone),
        "by_kind": dict(sorted(by_kind.items())),
        "attempts": views,
        "current": current,
        "last_ledger_line_age_seconds": (
            round(now - ledger_path.stat().st_mtime, 1) if ledger_path.is_file() else None
        ),
        "torn_tail": None if torn is None else {"offset": torn.offset, "length": torn.length},
        "seconds_median": medians,
        "eta_seconds": round(eta, 1),
        "eta_sources": sources,
        "eta_unknown_kinds": unknown,
        "host_load1_median": round(statistics.median(loads), 2) if loads else None,
        "abandoned_by_reason": dict(Counter(a.reason for a in gone)),
    }


def progress(run_id: str, now: float | None = None) -> dict[str, Any]:
    run = faultslib.open_run(run_id)
    at = time.time() if now is None else now
    fallback = fallback_medians()
    apps = {
        app: view
        for app in APPS
        if (view := app_progress(faultslib.run_dir(run_id, app), fallback.get(app, {}), at))
        is not None
    }
    return {"run_id": run_id, "plan": faultslib.run_plan(run), "created_at": run["created_at"],
            "apps": apps}  # fmt: skip


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d} min"


def render(p: dict[str, Any]) -> str:
    out = [f"run {p['run_id']} (plan {p['plan']}, created {p['created_at']})"]
    if not p["apps"]:
        out.append("  nothing prepared yet")
    for app, v in p["apps"].items():
        out += [
            "",
            f"{app}: {v['done']} of {v['entries']} entries done, {v['remaining']} remaining,"
            f" {v['abandoned']} traces abandoned",
            "  kind                       done  remaining  abandoned  median s",
        ]
        for kind, row in v["by_kind"].items():
            median = v["seconds_median"].get(kind, "-")
            out.append(
                f"  {kind:<26} {row['done']:>4}  {row['remaining']:>9}  {row['abandoned']:>9}"
                f"  {median:>8}"
            )
        for a in v["attempts"]:
            out.append(
                f"  attempt {a['id']}: {a['status']} (pid {a['pid']}), started {a['started_at']}"
                + (f", ended {a['ended_at']}" if a["ended_at"] else f", last: {a['last_event']}")
            )
        if not v["attempts"]:
            out.append("  no attempt yet")
        if v["current"] is not None:
            c = v["current"]
            out.append(f"  in flight: {c['entry_id']} for {c['seconds']} s")
        if v["last_ledger_line_age_seconds"] is not None:
            out.append(f"  last ledger line: {v['last_ledger_line_age_seconds']} s ago")
        if v["torn_tail"] is not None:
            out.append(
                f"  TORN LEDGER TAIL: {v['torn_tail']['length']} bytes at offset"
                f" {v['torn_tail']['offset']}; the next resume copies it aside and cuts it off"
            )
        if v["remaining"]:
            eta = f"  ETA: about {_duration(v['eta_seconds'])} of run time"
            fallback_kinds = [k for k, s in v["eta_sources"].items() if s != "this run"]
            if fallback_kinds:
                eta += f" (validation-run medians for: {', '.join(fallback_kinds)})"
            if v["eta_unknown_kinds"]:
                eta += f"; no median for: {', '.join(v['eta_unknown_kinds'])}"
            out.append(eta)
        if v["host_load1_median"] is not None:
            out.append(f"  host load (1 min) median: {v['host_load1_median']}")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=cast("str", __doc__).splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true", help="the progress as JSON")
    args = parser.parse_args()
    p = progress(args.run_id)
    sys.stdout.write(json.dumps(p, indent=2) + "\n" if args.json else render(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
