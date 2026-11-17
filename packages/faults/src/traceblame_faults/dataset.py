"""The labelled traces of an eval run: the one read of ground truth (#70).

It starts from eval_traces.trace_id and joins injections on (trace_id, injection_id)
(sql/eval_dataset_read.sql), never on the injection id alone: after a resume the spans of an
abandoned trace carry the same injection id as the completed one, and an injections row on
another trace must not label this one. A trace in eval_exclusions is never in the result; if
one has an eval_traces row the read fails instead of returning it.

Label per trace: "clean" (no fault planned), the injected component (applied), or none for a
planned fault that was not reached (status not_reached: neither clean nor faulted; #19 leaves
it out and counts it).

`trace_ids` is the input of `traceblame-detectors snapshot-export --trace-ids-file`: the
detectors read no label table (hard rule 9), so the list is made here.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from traceblame_faults import attempts, record


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Row:
    trace_id: str
    service: str
    source: str
    query_ref: str
    stratum: str
    planned_fault_kind: str
    injection_id: str
    status: str  # clean | applied | not_reached
    label: str | None  # "clean", the component, or None (not_reached)
    split: str  # "calib" | "test" | "" (no split given)


@dataclass(frozen=True)
class Dataset:
    eval_run_id: str
    rows: tuple[Row, ...]
    excluded: tuple[attempts.Abandoned, ...]

    @property
    def trace_ids(self) -> list[str]:
        return sorted(r.trace_id for r in self.rows)

    def counts(self) -> dict[str, Any]:
        """How many traces were read, by status, label and split, and how many are excluded."""
        return {
            "traces": len(self.rows),
            "by_status": dict(Counter(r.status for r in self.rows)),
            "by_label": dict(Counter(r.label or "not_reached" for r in self.rows)),
            "by_split": dict(Counter(r.split or "none" for r in self.rows)),
            "excluded": len(self.excluded),
            "excluded_by_reason": dict(Counter(a.reason for a in self.excluded)),
            "excluded_by_kind": dict(
                Counter(a.planned_fault_kind or "clean" for a in self.excluded)
            ),
        }


def read(
    client: record.Client, eval_run_id: str, *, splits: Mapping[str, str] | None = None
) -> Dataset:
    """The run's labelled traces. splits: query_ref -> "calib" | "test" (the plan's committed
    query list); with it, a trace whose query has no side is an error.

    Fails on: no trace at all, two rows for a trace, an injection id on two traces, an applied
    trace without its injections row or with contradicting ones, a trace that is excluded.
    """
    raw = client.query_rows(record.statement("eval_dataset_read.sql"), {"eval_run_id": eval_run_id})
    if not raw:
        raise DatasetError(f"{eval_run_id}: no eval_traces rows (nothing read)")
    excluded = record.exclusions(client, eval_run_id)
    gone = {a.trace_id for a in excluded}
    rows: list[Row] = []
    seen: set[str] = set()
    by_injection: dict[str, str] = {}
    for r in raw:
        tid = record.trace_id(r[0])
        service, source, query_ref, stratum, planned, injection_id = (str(v) for v in r[1:7])
        fault_kind, component, n, variants = str(r[7]), str(r[8]), int(r[9]), int(r[10])
        if tid in seen:
            raise DatasetError(f"{tid}: two eval_traces rows")
        seen.add(tid)
        if tid in gone:
            raise DatasetError(f"{tid}: in eval_exclusions and in eval_traces")
        if not planned:
            status, label = "clean", "clean"
            if injection_id:
                raise DatasetError(f"{tid}: a clean trace with an injection id")
        elif not injection_id:
            status, label = "not_reached", None
        else:
            if by_injection.setdefault(injection_id, tid) != tid:
                raise DatasetError(
                    f"{injection_id}: labels two traces ({by_injection[injection_id]}, {tid})"
                )
            if n == 0:
                raise DatasetError(f"{tid}: applied, and no injections row on this trace")
            if variants != 1 or fault_kind != planned:
                raise DatasetError(f"{tid}: its injections rows do not agree with eval_traces")
            status, label = "applied", component
        split = ""
        if splits is not None:
            if query_ref not in splits:
                raise DatasetError(f"{tid}: its query has no split side")
            split = splits[query_ref]
        rows.append(
            Row(
                tid,
                service,
                source,
                query_ref,
                stratum,
                planned,
                injection_id,
                status,
                label,
                split,
            )
        )
    return Dataset(eval_run_id, tuple(rows), tuple(excluded))


def labels(dataset: Dataset) -> list[dict[str, Any]]:
    """One JSON-able row per trace, ids and enums only."""
    return [
        {
            "trace_id": r.trace_id,
            "service": r.service,
            "query_ref": r.query_ref,
            "planned_fault_kind": r.planned_fault_kind,
            "status": r.status,
            "label": r.label,
            "split": r.split,
        }
        for r in dataset.rows
    ]


def stored_traces(
    client: record.Client, eval_run_id: str, service: str, since: str
) -> list[tuple[str, float, frozenset[str], int]]:
    """Every trace of the app stored under the run's marker since `since` ("YYYY-MM-DD
    HH:MM:SS", UTC): (trace_id, first span start in epoch seconds, injection markers, spans)."""
    rows = client.query_rows(
        record.statement("eval_run_traces_read.sql"),
        {"eval_run_id": eval_run_id, "service": service, "since": since},
    )
    return [
        (record.trace_id(r[0]), _epoch(r[1]), frozenset(str(m) for m in r[2]), int(r[3]))
        for r in rows
    ]


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=UTC).timestamp()


def injection_rows(
    client: record.Client, trace_ids: Sequence[str], injection_ids: Sequence[str]
) -> list[tuple[str, str, str]]:
    """(trace_id, injection_id, params) of every injections row on one of these traces or with
    one of these injection ids."""
    rows = client.query_rows(
        record.statement("eval_injections_by_id_read.sql"),
        {"trace_ids": sorted(set(trace_ids)), "injection_ids": sorted(set(injection_ids))},
    )
    return [(record.trace_id(r[0]), str(r[1]), str(r[2])) for r in rows]
