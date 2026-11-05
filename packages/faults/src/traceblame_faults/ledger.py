"""The injection ledger: one JSON line per eval trace, written in-process by the injector and
recorded into ClickHouse afterwards (record.py). Content-free by construction: ids, enums,
numbers and digests; query_ref is the dataset's own question or task id (#14 decision).

A trace is clean (no fault planned), applied (the fault fired: injection_id set, an injections
row follows) or not_reached (a fault was planned but its point never came, e.g. no k-th tool
call: no injections row, and #19 excludes it).
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

from traceblame_faults import ids, kinds

_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
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


def status(rec: Record) -> Status:
    if rec.injection_id:
        return "applied"
    return "not_reached" if rec.planned_fault_kind else "clean"


def _fail(rec: Record, msg: str) -> LedgerError:
    return LedgerError(f"ledger record {rec.trace_id!r}: {msg}")


def validate(rec: Record) -> None:
    if not _TRACE_ID.fullmatch(rec.trace_id):
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
    validate(rec)
    with path.open("a", encoding="utf-8") as f:
        f.write(dumps(rec) + "\n")


def read(path: Path) -> list[Record]:
    out: list[Record] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = loads(line)
            validate(rec)
        except (LedgerError, TypeError, ValueError) as exc:
            raise LedgerError(f"{path}: line {n}: {exc}") from exc
        out.append(rec)
    return out


def host_load1() -> float | None:
    """The host's 1-minute load average, rounded to 0.01; None where there is none."""
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def now_iso(now: datetime) -> str:
    """injected_at from an aware datetime."""
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
