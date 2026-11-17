"""The resumable loop of a fault run (#70), generic over what runs one plan entry.

One attempt at a time: an attempt holds flock on <run>/<app>/run.lock, and whatever it starts to
run the entries inherits the descriptor, so a driver that was killed alone still blocks a resume
until its child is gone.

open_attempt() is the only way in. For a run that has attempts already it

- refuses a provenance that differs from the first attempt's (no override: a new run id);
- declares attempts with no end killed;
- copies a torn ledger tail aside and cuts it off;
- records every entry that was started and has no ledger record as lost, and the trace ids
  known for it as abandoned (attempts.py). The entry runs again under a new trace id;
- stops the local helpers a dead attempt left behind.

An entry is complete only by its ledger line, which EntryLog.record() writes after the trace
has ended. The remaining entries run in plan order.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from traceblame_faults import appendlog, attempts, ledger, plan

LOCK = "run.lock"
LEDGER = "ledger.jsonl"
FIRST_PROVENANCE = "provenance.first.json"
_TAIL_ENTRY = re.compile(rb'"entry_id":"([A-Za-z0-9._:-]{1,128})"')
_TAIL_TRACE = re.compile(rb'"trace_id":"([0-9a-f]{32})"')
_MAX_SHOWN = 96

Salvage = Callable[[Sequence[attempts.Started]], Mapping[str, Sequence[str]]]


class RunnerError(RuntimeError):
    pass


class BusyError(RunnerError):
    """Another attempt of this run still holds the lock."""


class ProvenanceError(RunnerError):
    """The run's provenance changed since its first attempt."""


def lock(run_dir: Path) -> int:
    """flock on the run's lock file, or BusyError naming the attempt that holds it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(run_dir / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        running = [a for a in _events_attempts(run_dir) if a.status == "open"]
        who = (
            f"attempt {running[-1].id} still running, pid {running[-1].pid}"
            if running
            else "another process holds the run lock"
        )
        raise BusyError(f"{run_dir}: {who}") from exc
    return fd


def _events_attempts(run_dir: Path) -> list[attempts.Attempt]:
    try:
        return attempts.attempts(attempts.read(run_dir / attempts.FILE))
    except attempts.AttemptsError:
        return []


def digest(provenance: Mapping[str, Any]) -> str:
    text = json.dumps(provenance, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in cast("Mapping[str, Any]", value).items():
            out |= _flat(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    return {prefix: value}


def _shown(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= _MAX_SHOWN else text[:_MAX_SHOWN] + "..."


def differences(first: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """What differs between two provenances, by dotted key."""
    a, b = _flat(first), _flat(now)
    missing = object()
    out: list[str] = []
    for key in sorted(set(a) | set(b)):
        x, y = a.get(key, missing), b.get(key, missing)
        if x == y:
            continue
        was = "absent" if x is missing else _shown(x)
        out.append(f"{key}: {was} -> {'absent' if y is missing else _shown(y)}")
    return out


def proc_start(pid: int) -> int | None:
    """The process's start time in clock ticks (field 22 of /proc/<pid>/stat); None if there is
    no such process or it has exited (a zombie). With the pid it names one process, whatever
    pid reuse does later."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    return None if fields[0] == "Z" else int(fields[19])


def stop_helpers(
    events: Sequence[Mapping[str, Any]], *, keep: str | None = None, wait: float = 10.0
) -> list[dict[str, Any]]:
    """Stop the helper processes of earlier attempts that are still alive (a collector or a
    port-forward a killed driver left behind): SIGTERM, then SIGKILL. Returns what it stopped."""
    stopped: list[dict[str, Any]] = []
    for e in events:
        if e["event"] != "helper" or e.get("attempt_id") == keep:
            continue
        pid, started = int(e["pid"]), e.get("started")
        if started is None or proc_start(pid) != started:
            continue
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + wait
        while proc_start(pid) == started and time.monotonic() < deadline:
            time.sleep(0.05)
        if proc_start(pid) == started:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        stopped.append({"attempt_id": e.get("attempt_id"), "name": e.get("name"), "pid": pid})
    return stopped


@dataclass(frozen=True)
class Recovery:
    """What open_attempt found and recorded about earlier attempts."""

    killed: tuple[str, ...] = ()
    torn: appendlog.Tail | None = None
    lost: tuple[dict[str, Any], ...] = ()
    abandoned: tuple[attempts.Abandoned, ...] = ()
    helpers_stopped: tuple[dict[str, Any], ...] = ()


class EntryLog:
    """What the process that runs the entries writes: entry_start, the trace id as soon as it
    is known, and the ledger record once the trace has ended."""

    def __init__(self, run_dir: Path, attempt_id: str, eval_run_id: str) -> None:
        self.run_dir = run_dir
        self.attempt_id = attempt_id
        self.eval_run_id = eval_run_id
        self._log = attempts.Log(run_dir / attempts.FILE)
        self._writer = ledger.Writer(run_dir / LEDGER)
        self._traces: dict[str, list[str]] = {}

    def remaining(self, entries: Sequence[plan.Entry]) -> list[plan.Entry]:
        """The entries without a ledger record, in plan order."""
        done = self._writer.entries
        return [e for e in entries if e.entry_id not in done]

    def start(self, entry: plan.Entry) -> None:
        self._traces[entry.entry_id] = []
        self._log.append("entry_start", attempt_id=self.attempt_id, entry_id=entry.entry_id)

    def trace(self, entry: plan.Entry, trace_id: str) -> None:
        seen = self._traces.setdefault(entry.entry_id, [])
        if trace_id in seen:
            return
        if not ledger.TRACE_ID.fullmatch(trace_id):
            raise RunnerError(f"{entry.entry_id}: trace id {trace_id!r} is not 32 lowercase hex")
        seen.append(trace_id)
        self._log.append(
            "entry_trace", attempt_id=self.attempt_id, entry_id=entry.entry_id, trace_id=trace_id
        )

    def _abandon(self, entry: plan.Entry, trace_ids: Sequence[str]) -> None:
        for t in trace_ids:
            self._log.abandoned(
                attempts.Abandoned(
                    t, self.eval_run_id, entry.app, entry.entry_id, self.attempt_id,
                    entry.kind, entry.injection_id, "no_record", "attempt_log",
                )
            )  # fmt: skip

    def record(self, entry: plan.Entry, rec: ledger.Record) -> ledger.Record:
        """The entry's ledger line. Any other trace seen for the entry is abandoned."""
        if rec.eval_run_id != self.eval_run_id:
            raise RunnerError(f"{entry.entry_id}: a record of run {rec.eval_run_id}")
        rec = replace(rec, entry_id=entry.entry_id, attempt_id=self.attempt_id)
        self._writer.append(rec)
        self._abandon(entry, [t for t in self._traces.get(entry.entry_id, []) if t != rec.trace_id])
        return rec

    def no_record(self, entry: plan.Entry, status: str, trace_ids: Sequence[str] = ()) -> None:
        """The entry ended without a record (no trace, or more than one): it stays incomplete
        and a resume runs it again; the traces it left are abandoned."""
        known = self._traces.get(entry.entry_id, [])
        self._abandon(entry, [*known, *[t for t in trace_ids if t not in known]])
        self._log.append(
            "entry_end", attempt_id=self.attempt_id, entry_id=entry.entry_id, status=status
        )


RunOne = Callable[[plan.Entry, EntryLog], ledger.Record | None]


def run_entries(log: EntryLog, entries: Sequence[plan.Entry], run_one: RunOne) -> int:
    """Every remaining entry through run_one, in plan order; how many ran."""
    remaining = log.remaining(entries)
    for entry in remaining:
        log.start(entry)
        rec = run_one(entry, log)
        if rec is None:
            log.no_record(entry, "no_trace")
        else:
            log.record(entry, rec)
    return len(remaining)


@dataclass
class Attempt:
    id: str
    run_dir: Path
    eval_run_id: str
    lock_fd: int
    remaining: list[plan.Entry]
    recovery: Recovery
    _log: attempts.Log = field(repr=False)
    _ended: bool = False

    def entry_log(self) -> EntryLog:
        return EntryLog(self.run_dir, self.id, self.eval_run_id)

    def helper(self, name: str, pid: int) -> None:
        """A local helper process of this attempt, so that a later attempt can stop it."""
        self._log.append("helper", attempt_id=self.id, name=name, pid=pid, started=proc_start(pid))

    def note(self, **fields: Any) -> None:
        """Driver facts of the attempt (kept out of the events the recovery reads)."""
        path = self.run_dir / f"attempt.{self.id}.json"
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        write_atomic(path, json.dumps(doc | fields, indent=2, default=str) + "\n")

    def end(self, status: str = "completed") -> None:
        if self._ended:
            return
        self._ended = True
        try:
            self._log.append("attempt_end", attempt_id=self.id, status=status)
        finally:
            os.close(self.lock_fd)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _tail_ids(raw: bytes) -> tuple[str | None, str | None]:
    entry, trace = _TAIL_ENTRY.search(raw), _TAIL_TRACE.search(raw)
    return (
        entry.group(1).decode() if entry else None,
        trace.group(1).decode() if trace else None,
    )


def _recover(
    run_dir: Path,
    entries: Sequence[plan.Entry],
    events: Sequence[Mapping[str, Any]],
    log: attempts.Log,
    attempt_id: str,
    eval_run_id: str,
    salvage: Salvage | None,
) -> Recovery:
    by_id = {e.entry_id: e for e in entries}
    statuses = {a.id: a for a in attempts.attempts(events)}
    killed: list[str] = []
    for a in statuses.values():
        if a.status == "open":
            log.append(
                "attempt_killed",
                attempt_id=a.id,
                status="killed/unknown",
                last_event=a.last_event,
                detected_by=attempt_id,
            )
            killed.append(a.id)
    copy = run_dir / f"ledger.torn.{attempt_id}"
    torn = ledger.repair(run_dir / LEDGER, copy)
    torn_entry: str | None = None
    torn_trace: str | None = None
    if torn is not None:
        torn_entry, torn_trace = _tail_ids(copy.read_bytes())
        log.append(
            "torn_tail",
            attempt_id=attempt_id,
            file=LEDGER,
            copy=copy.name,
            offset=torn.offset,
            length=torn.length,
            sha256=torn.sha256,
            entry_id=torn_entry or "",
            trace_id=torn_trace or "",
        )
    done = {(r.entry_id, r.attempt_id) for r in ledger.read(run_dir / LEDGER)}
    pending = [
        s
        for s in attempts.started(events)
        if not s.ended and (s.entry_id, s.attempt_id) not in done
    ]
    extra: Mapping[str, Sequence[str]] = salvage(pending) if salvage is not None and pending else {}
    known = {a.trace_id for a in attempts.abandoned(events)}
    lost: list[dict[str, Any]] = []
    abandoned: list[attempts.Abandoned] = []
    for s in pending:
        entry = by_id.get(s.entry_id)
        if entry is None:
            raise RunnerError(f"{run_dir}: the attempt log names {s.entry_id}, the plan does not")
        is_torn = torn is not None and (
            torn_entry == s.entry_id or (torn_entry is None and s is pending[-1])
        )
        status = statuses[s.attempt_id].status if s.attempt_id in statuses else "open"
        reason = "torn_tail" if is_torn else ("failed" if status.startswith("failed") else "killed")
        sources = dict.fromkeys(s.trace_ids, "attempt_log")
        if is_torn and torn_trace is not None:
            sources.setdefault(torn_trace, "attempt_log")
        for t in extra.get(s.entry_id, ()):
            sources.setdefault(t, "preload")
        for t, source in sources.items():
            if t in known:
                continue
            a = attempts.Abandoned(
                t, eval_run_id, entry.app, entry.entry_id, s.attempt_id,
                entry.kind, entry.injection_id, reason, source,
            )  # fmt: skip
            log.abandoned(a)
            abandoned.append(a)
            known.add(t)
        lost.append(
            log.append(
                "entry_lost",
                attempt_id=s.attempt_id,
                entry_id=s.entry_id,
                reason=reason,
                started_at=s.at,
                trace_ids=list(sources),
                detected_by=attempt_id,
            )
        )
    stopped = stop_helpers(events)
    return Recovery(tuple(killed), torn, tuple(lost), tuple(abandoned), tuple(stopped))


def open_attempt(
    run_dir: Path,
    entries: Sequence[plan.Entry],
    *,
    eval_run_id: str,
    provenance: Mapping[str, Any],
    salvage: Salvage | None = None,
    now: datetime | None = None,
    **facts: Any,
) -> Attempt:
    """Start an attempt of the run in run_dir, or resume it (see the module docstring).

    salvage: called with the entries found in flight before they are recorded as lost; returns
    further trace ids per entry_id (the runbook driver reads them from the sandbox). facts go
    into the attempt_start event.
    """
    plan.check_entries(list(entries))
    fd = lock(run_dir)
    try:
        path = run_dir / attempts.FILE
        stamp = f"{(now or datetime.now().astimezone()):%Y%m%dT%H%M%S%f}"
        own_tail = appendlog.repair(path, run_dir / f"attempts.torn.{stamp}")
        events = attempts.read(path)
        attempt_id = attempts.next_id(events, now)
        log = attempts.Log(path)
        if own_tail is not None:
            log.append(
                "torn_tail",
                attempt_id=attempt_id,
                file=attempts.FILE,
                copy=f"attempts.torn.{stamp}",
                offset=own_tail.offset,
                length=own_tail.length,
                sha256=own_tail.sha256,
            )
        first = run_dir / FIRST_PROVENANCE
        had_attempts = any(e["event"] == "attempt_start" for e in events)
        if first.is_file():
            diffs = differences(json.loads(first.read_text(encoding="utf-8")), provenance)
            if diffs:
                log.append("resume_refused", attempt_id=attempt_id, differences=diffs)
                raise ProvenanceError(
                    f"{run_dir}: the provenance changed since the first attempt; start a new run"
                    f" (a new run id). Changed: " + "; ".join(diffs)
                )
        elif had_attempts or (run_dir / LEDGER).is_file():
            raise RunnerError(
                f"{run_dir}: attempts or a ledger without {FIRST_PROVENANCE}: not a resumable run"
            )
        else:
            write_atomic(
                first, json.dumps(provenance, indent=2, sort_keys=True, default=str) + "\n"
            )
        recovery = _recover(run_dir, entries, events, log, attempt_id, eval_run_id, salvage)
        done = ledger.Writer(run_dir / LEDGER).entries
        unknown = done - {e.entry_id for e in entries}
        if unknown:
            raise RunnerError(
                f"{run_dir}: the ledger has entries the plan does not: {sorted(unknown)}"
            )
        remaining = [e for e in entries if e.entry_id not in done]
        log.append(
            "attempt_start",
            attempt_id=attempt_id,
            pid=os.getpid(),
            provenance_digest=digest(provenance),
            entries=len(entries),
            remaining=len(remaining),
            **facts,
        )
        return Attempt(attempt_id, run_dir, eval_run_id, fd, remaining, recovery, log)
    except BaseException:
        os.close(fd)
        raise
