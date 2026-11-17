"""The injection ledger: one JSON line per eval trace, written in-process by the injector and
recorded into ClickHouse afterwards (record.py). Content-free by construction: ids, enums,
numbers and digests; query_ref is the dataset's own question or task id (#14 decision).

A trace is clean (no fault planned), applied (the fault fired: injection_id set, an injections
row follows) or not_reached (a fault was planned but its point never came, e.g. no k-th tool
call: no injections row, and #19 excludes it).

A line is appended only after its trace has ended, durably (appendlog), and the file is never
rewritten: it is what `record` writes into eval_traces, so a trace that was killed mid-flight
never gets a line or a row (#70). A record names the plan entry it completes; a Writer refuses a
second record for an entry, and every reader refuses a torn tail.
"""

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from traceblame_faults import appendlog, ids, kinds

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_INJECTED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_MAX_QUERY_REF = 512
GOLDEN_STATUSES = ("mapped", "unmapped", "none")
SOURCES = ("cuad", "runbook")

Status = Literal["clean", "applied", "not_reached"]


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    trace_id: str
    eval_run_id: str
    service: str
    source: str
    query_ref: str
    stratum: str
    golden_doc_ids: tuple[str, ...]
    golden_status: str
    planned_fault_kind: str
    injection_id: str
    fault_kind: str
    target_component: str
    params: Mapping[str, Any] = field(default_factory=dict[str, Any])
    seed: int = 0
    injected_at: str | None = None  # UTC, millisecond ISO 8601 with Z
    bundle_digest: str | None = None  # the aegis.bundle.digest the trace ran under
    # The host's 1-minute load average when the trace ended (os.getloadavg; None where the
    # platform has none, and in ledgers written before the field). Wall-time context for the
    # D1 throughput numbers; never recorded into ClickHouse.
    host_load1: float | None = None
    # The plan entry this record completes (ids.entry_id) and the attempt that ran it (#70);
    # None in ledgers written before them. Never recorded into ClickHouse.
    entry_id: str | None = None
    attempt_id: str | None = None


def status(rec: Record) -> Status:
    if rec.injection_id:
        return "applied"
    return "not_reached" if rec.planned_fault_kind else "clean"


def _fail(rec: Record, msg: str) -> LedgerError:
    return LedgerError(f"ledger record {rec.trace_id!r}: {msg}")


def validate(rec: Record) -> None:
    if not TRACE_ID.fullmatch(rec.trace_id):
        raise _fail(rec, "trace_id is not 32 lowercase hex")
    try:
        ids.check(rec.eval_run_id, "eval_run_id")
    except ids.IdError as exc:
        raise _fail(rec, str(exc)) from exc
    if rec.service not in kinds.APPS:
        raise _fail(rec, f"service {rec.service!r}")
    if rec.source not in SOURCES:
        raise _fail(rec, f"source {rec.source!r}")
    if not rec.query_ref or len(rec.query_ref) > _MAX_QUERY_REF or not rec.query_ref.isprintable():
        raise _fail(rec, "query_ref must be a printable dataset id")
    if not ids.PATTERN.fullmatch(rec.stratum):
        raise _fail(rec, f"stratum {rec.stratum!r}")
    if rec.golden_status not in GOLDEN_STATUSES:
        raise _fail(rec, f"golden_status {rec.golden_status!r}")
    if not all(ids.PATTERN.fullmatch(d) for d in rec.golden_doc_ids):
        raise _fail(rec, "golden_doc_ids must be ids")
    if bool(rec.golden_doc_ids) != (rec.golden_status == "mapped"):
        raise _fail(rec, "golden_doc_ids are set exactly when golden_status is mapped")
    if rec.bundle_digest is not None and not kinds.DIGEST_PATTERN.fullmatch(rec.bundle_digest):
        raise _fail(rec, "bundle_digest is not a sha256 digest")
    if rec.host_load1 is not None and not (math.isfinite(rec.host_load1) and rec.host_load1 >= 0):
        raise _fail(rec, "host_load1 must be a finite load average >= 0")
    if rec.planned_fault_kind and rec.planned_fault_kind not in kinds.KINDS:
        raise _fail(rec, f"planned_fault_kind {rec.planned_fault_kind!r}")
    if rec.entry_id is not None and rec.entry_id != ids.entry_id(
        rec.service, rec.planned_fault_kind, rec.query_ref
    ):
        raise _fail(rec, "entry_id does not derive from the service, planned kind and query")
    if rec.attempt_id is not None and not ids.PATTERN.fullmatch(rec.attempt_id):
        raise _fail(rec, f"attempt_id {rec.attempt_id!r}")
    if not rec.injection_id:
        if rec.fault_kind or rec.target_component or rec.params or rec.injected_at:
            raise _fail(rec, "fault_kind, target_component, params, injected_at need an injection")
        return
    if rec.fault_kind != rec.planned_fault_kind:
        raise _fail(rec, "fault_kind differs from planned_fault_kind")
    kind = kinds.KINDS[rec.fault_kind]
    if rec.target_component != kind.component:
        raise _fail(rec, f"target_component {rec.target_component!r} is not {kind.component}")
    if rec.injection_id != ids.injection_id(rec.eval_run_id, rec.fault_kind, rec.query_ref):
        raise _fail(rec, "injection_id does not derive from the run, kind and query")
    if rec.injected_at is None or not _INJECTED_AT.fullmatch(rec.injected_at):
        raise _fail(rec, "injected_at must be UTC ISO 8601 with milliseconds")
    try:
        kinds.check_params(rec.fault_kind, rec.params, applied=True)
    except kinds.ParamsError as exc:
        raise _fail(rec, f"params: {exc}") from exc


def dumps(rec: Record) -> str:
    return json.dumps(asdict(rec), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def loads(line: str) -> Record:
    d = json.loads(line)
    d["golden_doc_ids"] = tuple(d.get("golden_doc_ids", ()))
    return Record(**d)


def append(path: Path, rec: Record) -> None:
    """One validated record, durably (appendlog.append). The drivers go through a Writer, which
    also keeps one record per plan entry."""
    validate(rec)
    try:
        appendlog.append(path, dumps(rec))
    except appendlog.TornTailError as exc:
        raise LedgerError(str(exc)) from exc


def read(path: Path) -> list[Record]:
    """Every record of the ledger; none for a missing file. A torn tail, a malformed line and two
    records for one plan entry are refused."""
    try:
        lines = appendlog.lines(path)
    except appendlog.TornTailError as exc:
        raise LedgerError(str(exc)) from exc
    out: list[Record] = []
    seen: set[str] = set()
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            rec = loads(line)
            validate(rec)
        except (LedgerError, TypeError, ValueError) as exc:
            raise LedgerError(f"{path}: line {n}: {exc}") from exc
        if rec.entry_id is not None:
            if rec.entry_id in seen:
                raise LedgerError(f"{path}: line {n}: two records for entry {rec.entry_id}")
            seen.add(rec.entry_id)
        out.append(rec)
    return out


class Writer:
    """The run's ledger for one attempt: appends only, one record per plan entry."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries = {r.entry_id for r in read(path) if r.entry_id is not None}

    @property
    def entries(self) -> frozenset[str]:
        """The plan entries that have their record."""
        return frozenset(self._entries)

    def append(self, rec: Record) -> None:
        if rec.entry_id is None:
            raise LedgerError(f"ledger record {rec.trace_id!r}: a run's record needs its entry_id")
        if rec.entry_id in self._entries:
            raise LedgerError(f"{self.path}: entry {rec.entry_id} already has a record")
        append(self.path, rec)
        self._entries.add(rec.entry_id)


def torn(path: Path) -> appendlog.Tail | None:
    """The ledger's torn tail, if it has one."""
    return appendlog.tail(path)


def repair(path: Path, copy: Path) -> appendlog.Tail | None:
    """Resume only: the torn tail copied to `copy` and cut off."""
    return appendlog.repair(path, copy)


def host_load1() -> float | None:
    """The host's 1-minute load average, rounded to 0.01; None where there is none."""
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def now_iso(now: datetime) -> str:
    """injected_at from an aware datetime."""
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
