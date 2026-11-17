"""What must hold after a resumable run (#70), as a pure function of the plan, the ledger, the
attempt log and what is stored. The drivers' check phase feeds it from ClickHouse; the SIGKILL
test feeds it from a span file.

- Per plan entry: exactly one ledger record and one eval_traces row; per applied entry exactly
  one injections row, on that record's trace.
- Per injection id: of the stored traces that carry it, exactly one is completed; any other is
  abandoned (and in eval_exclusions).
- No orphans: every stored trace of the run is completed or abandoned. A stored trace that is
  neither is attributed to an entry that was started and not completed, by its marker or by
  its start time falling into exactly one such entry's window; it is then abandoned with
  source orphan_sweep (returned in `sweep` for the caller to record). Anything else fails.
- An abandoned trace is never in the ledger or in eval_traces.
- Zero checked = fail: no entries, no stored traces or no eval_traces rows.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from traceblame_faults import attempts, ledger, plan


@dataclass(frozen=True)
class StoredTrace:
    """One trace stored under the run's aegis.eval.run.id."""

    trace_id: str
    first_start: float  # epoch seconds of its earliest stored span
    markers: frozenset[str]  # the aegis.fault.injection.id values on its spans
    spans: int


@dataclass(frozen=True)
class _Window:
    """One started entry that did not complete: when it ran, and why it ended."""

    attempt_id: str
    entry_id: str
    start: float
    end: float
    reason: str


@dataclass
class Result:
    problems: list[str]
    summary: dict[str, Any]
    sweep: list[attempts.Abandoned] = field(default_factory=list[attempts.Abandoned])


def _windows(events: Sequence[Mapping[str, Any]]) -> list[_Window]:
    start: dict[tuple[str, str], float] = {}
    out: list[_Window] = []
    for e in events:
        key = (str(e.get("attempt_id", "")), str(e.get("entry_id", "")))
        if e["event"] == "entry_start":
            start[key] = attempts.seconds(str(e["at"]))
        elif e["event"] in ("entry_end", "entry_lost") and key in start:
            reason = str(e["reason"]) if e["event"] == "entry_lost" else "no_record"
            out.append(_Window(key[0], key[1], start[key], attempts.seconds(str(e["at"])), reason))
    return out


def _attribute(
    trace: StoredTrace,
    windows: Sequence[_Window],
    injection_of: Mapping[str, str],
    slack: float,
) -> _Window | None:
    """The one incomplete entry a stored trace belongs to, or None."""
    by_marker = [w for w in windows if injection_of.get(w.entry_id, "") in trace.markers]
    inside = [w for w in windows if w.start - slack <= trace.first_start <= w.end + slack]
    if by_marker:
        timed = [w for w in by_marker if w in inside]
        chosen = timed or by_marker
        return chosen[-1] if len({w.entry_id for w in chosen}) == 1 else None
    if trace.markers:
        return None  # it carries the marker of an entry that never lost a trace
    return inside[0] if len(inside) == 1 else None


def validate(
    *,
    eval_run_id: str,
    entries: Sequence[plan.Entry],
    records: Sequence[ledger.Record],
    events: Sequence[Mapping[str, Any]],
    eval_rows: Sequence[tuple[str, str, str]],
    injections: Sequence[tuple[str, str]],
    exclusions: Sequence[str],
    stored: Sequence[StoredTrace],
    slack_seconds: float = 5.0,
) -> Result:
    """eval_rows: (trace_id, planned_fault_kind, injection_id) of the run's eval_traces rows.
    injections: (trace_id, injection_id) of every injections row on a ledger trace or with one
    of the plan's injection ids. exclusions: the run's eval_exclusions trace ids."""
    problems: list[str] = []
    if not entries:
        problems.append("nothing checked: the plan has no entries")
    if not stored:
        problems.append("nothing checked: no stored trace of the run was read")
    if not eval_rows:
        problems.append("nothing checked: no eval_traces row of the run")

    by_entry: dict[str, list[ledger.Record]] = {}
    for r in records:
        if r.entry_id is None:
            problems.append(f"{r.trace_id}: a ledger record without an entry_id")
        else:
            by_entry.setdefault(r.entry_id, []).append(r)
    planned = {e.entry_id: e for e in entries}
    for entry_id in sorted(set(by_entry) - set(planned)):
        problems.append(f"{entry_id}: a ledger record for an entry the plan does not have")
    completed = {r.trace_id for r in records}
    eval_by_trace: dict[str, list[tuple[str, str, str]]] = {}
    for row in eval_rows:
        eval_by_trace.setdefault(row[0], []).append(row)
    for trace_id in sorted(set(eval_by_trace) - completed):
        problems.append(f"{trace_id}: an eval_traces row without a ledger record")
    injection_traces: dict[str, list[str]] = {}
    for trace_id, injection_id in injections:
        injection_traces.setdefault(injection_id, []).append(trace_id)

    applied: dict[str, ledger.Record] = {}
    for entry in entries:
        recs = by_entry.get(entry.entry_id, [])
        if len(recs) != 1:
            what = "no ledger record (the run is not finished)" if not recs else "ledger records"
            problems.append(f"{entry.entry_id}: {len(recs) or ''} {what}".replace(":  ", ": "))
            continue
        rec = recs[0]
        rows = eval_by_trace.get(rec.trace_id, [])
        if len(rows) != 1 or rows[0][1:] != (rec.planned_fault_kind, rec.injection_id):
            problems.append(f"{entry.entry_id}: {len(rows)} eval_traces rows, or a different one")
        if ledger.status(rec) != "applied":
            continue
        applied[rec.injection_id] = rec
        on = injection_traces.get(rec.injection_id, [])
        if on != [rec.trace_id]:
            problems.append(
                f"{entry.entry_id}: injections rows of {rec.injection_id} on {len(on)} traces"
                f" ({on.count(rec.trace_id)} on its own)"
            )

    abandoned = {a.trace_id: a for a in attempts.abandoned(events)}
    for trace_id in sorted(set(abandoned) & completed):
        problems.append(f"{trace_id}: abandoned and in the ledger")
    excluded = set(exclusions)
    for trace_id in sorted(excluded & set(eval_by_trace)):
        problems.append(f"{trace_id}: in eval_exclusions and in eval_traces")
    for trace_id in sorted(set(abandoned) - excluded):
        problems.append(f"{trace_id}: abandoned, not in eval_exclusions (run record)")
    for trace_id in sorted(excluded - set(abandoned)):
        problems.append(f"{trace_id}: in eval_exclusions without an abandoned event")

    # Orphans: stored, neither completed nor abandoned.
    windows = _windows(events)
    injection_of = {e.entry_id: e.injection_id for e in entries if e.injection_id}
    sweep: list[attempts.Abandoned] = []
    orphans: list[str] = []
    for trace in sorted(stored, key=lambda t: t.trace_id):
        if trace.trace_id in completed or trace.trace_id in abandoned:
            continue
        w = _attribute(trace, windows, injection_of, slack_seconds)
        entry = planned.get(w.entry_id) if w is not None else None
        if w is None or entry is None:
            orphans.append(trace.trace_id)
            problems.append(
                f"{trace.trace_id}: an orphan: stored with the run's marker, neither completed"
                " nor abandoned, and not attributable to an entry in flight"
            )
            continue
        sweep.append(
            attempts.Abandoned(
                trace.trace_id,
                eval_run_id,
                entry.app,
                entry.entry_id,
                w.attempt_id,
                entry.kind,
                entry.injection_id,
                w.reason,
                "orphan_sweep",
            )
        )
    gone = abandoned | {a.trace_id: a for a in sweep}

    # Per injection id: one completed trace among the stored ones that carry it.
    marked: dict[str, list[str]] = {}
    for trace in stored:
        for marker in trace.markers:
            marked.setdefault(marker, []).append(trace.trace_id)
    checked_injections = 0
    for injection_id in sorted(set(marked) | set(applied)):
        checked_injections += 1
        traces = marked.get(injection_id, [])
        done = [t for t in traces if t in completed]
        rec = applied.get(injection_id)
        if rec is None:
            if done:
                problems.append(f"{injection_id}: on a completed trace that did not apply it")
        elif done != [rec.trace_id]:
            problems.append(
                f"{injection_id}: {len(done)} completed traces carry it, not exactly its record's"
            )
        for t in traces:
            if t not in completed and t not in gone and t not in orphans:
                problems.append(f"{injection_id}: also on {t}, which is not abandoned")

    stored_spans = {t.trace_id: t.spans for t in stored}
    summary: dict[str, Any] = {
        "entries": len(entries),
        "records": len(records),
        "eval_traces_rows": len(eval_rows),
        "stored_traces": len(stored),
        "injection_ids_checked": checked_injections,
        "attempts": [
            {
                "id": a.id,
                "status": a.status,
                "started_at": a.started_at,
                "ended_at": a.ended_at,
                "last_event": a.last_event,
            }
            for a in attempts.attempts(events)
        ],
        "killed": sum(a.status == "killed" for a in attempts.attempts(events)),
        "torn_tails": sum(e["event"] == "torn_tail" for e in events),
        "lost_entries": len(attempts.lost(events)),
        "abandoned": [
            {
                "trace_id": a.trace_id,
                "entry_id": a.entry_id,
                "attempt_id": a.attempt_id,
                "kind": a.planned_fault_kind or "clean",
                "reason": a.reason,
                "source": a.source,
                "stored_spans": stored_spans.get(a.trace_id, 0),
            }
            for a in gone.values()
        ],
        "abandoned_by_kind": dict(Counter(a.planned_fault_kind or "clean" for a in gone.values())),
        "abandoned_by_reason": dict(Counter(a.reason for a in gone.values())),
        "swept": len(sweep),
        "duplicate_injections": sum(len(set(traces)) > 1 for traces in injection_traces.values()),
        "orphans": orphans,
    }
    return Result(problems, summary, sweep)
