"""The attempt log of a resumable run (#70): attempts.jsonl, append-only and fsynced like the
ledger, one JSON line per event. Content-free: ids, pids, times, digests.

  attempt_start   attempt_id, pid, provenance_digest (+ whatever the driver adds)
  helper          attempt_id, name, pid, started: a local helper process of the attempt
  entry_start     attempt_id, entry_id
  entry_trace     attempt_id, entry_id, trace_id: as soon as the trace id is known
  entry_end       attempt_id, entry_id, status: an entry that ended without a ledger record
  attempt_end     attempt_id, status: "completed" | "failed: <reason>"
  attempt_killed  attempt_id, last_event: written by the next attempt for one with no end
  torn_tail       attempt_id, file, copy, offset, length, sha256
  entry_lost      attempt_id (the one that ran it), entry_id, reason, trace_ids: an entry that
                  was started and has neither a record nor an entry_end; it runs again
  abandoned       one trace that never completed an entry: trace_id, entry_id, attempt_id,
                  reason, source. `record` writes these into eval_exclusions, never into
                  eval_traces or injections
  resume_refused  why a resume did not start (a provenance change)

An entry is complete only by its ledger line; nothing here completes one.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from traceblame_faults import appendlog, ids, ledger

FILE = "attempts.jsonl"
EVENTS = frozenset(
    {
        "attempt_start",
        "helper",
        "entry_start",
        "entry_trace",
        "entry_end",
        "attempt_end",
        "attempt_killed",
        "torn_tail",
        "entry_lost",
        "abandoned",
        "resume_refused",
    }
)
# Why a trace is abandoned, and where its trace id came from.
REASONS = ("killed", "failed", "torn_tail", "no_record")
SOURCES = ("attempt_log", "preload", "orphan_sweep")


class AttemptsError(ValueError):
    pass


@dataclass(frozen=True)
class Abandoned:
    """A trace that belongs to the run and completed no plan entry."""

    trace_id: str
    eval_run_id: str
    service: str
    entry_id: str
    attempt_id: str
    planned_fault_kind: str
    injection_id: str
    reason: str
    source: str


def check_abandoned(a: Abandoned) -> Abandoned:
    if not ledger.TRACE_ID.fullmatch(a.trace_id):
        raise AttemptsError(f"abandoned {a.trace_id!r}: trace_id is not 32 lowercase hex")
    for value, what in (
        (a.eval_run_id, "eval_run_id"),
        (a.entry_id, "entry_id"),
        (a.attempt_id, "attempt_id"),
    ):
        if not ids.PATTERN.fullmatch(value):
            raise AttemptsError(f"abandoned {a.trace_id}: {what} {value!r}")
    if a.injection_id and not ids.PATTERN.fullmatch(a.injection_id):
        raise AttemptsError(f"abandoned {a.trace_id}: injection_id {a.injection_id!r}")
    if a.reason not in REASONS or a.source not in SOURCES:
        raise AttemptsError(f"abandoned {a.trace_id}: reason {a.reason!r}, source {a.source!r}")
    return a


def stamp(now: datetime | None = None) -> str:
    """UTC ISO 8601 with milliseconds, as the ledger's injected_at."""
    return ledger.now_iso((now or datetime.now(UTC)).astimezone(UTC))


def seconds(at: str) -> float:
    """An event's `at` as epoch seconds."""
    return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()


class Log:
    """The append side of attempts.jsonl."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        if event not in EVENTS:
            raise AttemptsError(f"unknown event {event!r}")
        doc: dict[str, Any] = {"event": event, "at": stamp(), **fields}
        try:
            appendlog.append(
                self.path, json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            )
        except appendlog.TornTailError as exc:
            raise AttemptsError(str(exc)) from exc
        return doc

    def abandoned(self, a: Abandoned) -> dict[str, Any]:
        return self.append("abandoned", **asdict(check_abandoned(a)))


def read(path: Path) -> list[dict[str, Any]]:
    """Every event; none for a missing file. A torn tail and an unknown event are refused."""
    try:
        lines = appendlog.lines(path)
    except appendlog.TornTailError as exc:
        raise AttemptsError(str(exc)) from exc
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            parsed: Any = json.loads(line)
        except ValueError as exc:
            raise AttemptsError(f"{path}: line {n}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AttemptsError(f"{path}: line {n}: not an attempt event")
        doc = cast("dict[str, Any]", parsed)
        if doc.get("event") not in EVENTS or "at" not in doc:
            raise AttemptsError(f"{path}: line {n}: not an attempt event")
        out.append(doc)
    return out


@dataclass(frozen=True)
class Attempt:
    id: str
    pid: int
    started_at: str
    provenance_digest: str
    status: str  # "open" | "completed" | "failed: ..." | "killed"
    ended_at: str | None
    last_event: str


def attempts(events: Sequence[Mapping[str, Any]]) -> list[Attempt]:
    """Every attempt with its status, in the order they started. "open" = no end and not yet
    declared killed: running, or dead and not resumed yet."""
    out: dict[str, Attempt] = {}
    for e in events:
        aid = str(e.get("attempt_id", ""))
        if e["event"] == "attempt_start":
            out[aid] = Attempt(
                aid,
                int(e["pid"]),
                str(e["at"]),
                str(e.get("provenance_digest", "")),
                "open",
                None,
                "attempt_start",
            )
        elif aid in out and e["event"] in ("attempt_end", "attempt_killed"):
            status = str(e["status"]) if e["event"] == "attempt_end" else "killed"
            out[aid] = replace(out[aid], status=status, ended_at=str(e["at"]))
        elif (
            aid in out
            and out[aid].status == "open"
            and e["event"] in ("entry_start", "entry_trace", "entry_end", "helper")
        ):
            last = f"{e['event']} {e.get('entry_id', e.get('name', ''))}".strip()
            out[aid] = replace(out[aid], last_event=last)
    return list(out.values())


def next_id(events: Sequence[Mapping[str, Any]], now: datetime | None = None) -> str:
    """a<n>-<UTC time>: n counts the attempts of this app's run."""
    n = sum(e["event"] == "attempt_start" for e in events) + 1
    return ids.check(
        f"a{n:03d}-{(now or datetime.now(UTC)).astimezone(UTC):%Y%m%dT%H%M%SZ}", "attempt_id"
    )


def abandoned(events: Sequence[Mapping[str, Any]]) -> list[Abandoned]:
    """Every abandoned trace, once, in the order recorded."""
    out: dict[str, Abandoned] = {}
    fields = Abandoned.__dataclass_fields__
    for e in events:
        if e["event"] == "abandoned":
            a = check_abandoned(Abandoned(**{k: e[k] for k in fields}))
            out.setdefault(a.trace_id, a)
    return list(out.values())


@dataclass(frozen=True)
class Started:
    """One entry_start of one attempt, with the trace ids known for it."""

    attempt_id: str
    entry_id: str
    at: str
    trace_ids: tuple[str, ...]
    ended: bool  # an entry_end or entry_lost event exists for it


def started(events: Sequence[Mapping[str, Any]]) -> list[Started]:
    order: list[tuple[str, str]] = []
    at: dict[tuple[str, str], str] = {}
    traces: dict[tuple[str, str], list[str]] = {}
    ended: set[tuple[str, str]] = set()
    for e in events:
        key = (str(e.get("attempt_id", "")), str(e.get("entry_id", "")))
        if e["event"] == "entry_start":
            if key not in at:
                order.append(key)
            at[key] = str(e["at"])
        elif e["event"] == "entry_trace":
            if str(e["trace_id"]) not in traces.setdefault(key, []):
                traces[key].append(str(e["trace_id"]))
        elif e["event"] in ("entry_end", "entry_lost"):
            ended.add(key)
    return [Started(a, entry, at[a, entry], tuple(traces.get((a, entry), ())), (a, entry) in ended)
            for a, entry in order]  # fmt: skip


def lost(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The entry_lost events: entries that were started, not completed, and run again."""
    return [dict(e) for e in events if e["event"] == "entry_lost"]
