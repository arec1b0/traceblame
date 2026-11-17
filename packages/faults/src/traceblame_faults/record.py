"""Ground truth into ClickHouse: a ledger into eval_traces (every trace) and injections (applied
faults only), and a run's abandoned traces into eval_exclusions, through the statements of the
storage contract.

- injections: contract/injections_write.sql of traceblame-storage. It inserts only when no row
  with the same (trace_id, injection_id) exists, so re-recording a ledger adds nothing.
- eval_traces, eval_exclusions and the eval reads: their statements are not contract statements
  yet (code N runs on schema N-1). The canonical copies are tests/integration/eval_*.sql; this
  package embeds them (sql/), and a contract test keeps both equal.

When a row is written: only here, after the run, one eval_traces row per ledger line, and a
ledger line exists only for a trace that has ended (ledger.py). A trace that was abandoned
(attempts.py) never gets an eval_traces or injections row: record() refuses a ledger that names
one, and writes it to eval_exclusions instead (#70).

Needs the `store` extra (traceblame-storage). A schema without eval_traces is refused before
anything is written: run `traceblame-storage migrate` first. So is a schema without
eval_exclusions when there are abandoned traces to record; without any, record() runs on it
(schema N-1).
"""

from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any, Protocol

from traceblame_faults import attempts, kinds, ledger


class RecordError(RuntimeError):
    pass


class Client(Protocol):
    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object: ...

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]: ...


_TABLES = (
    "SELECT name FROM system.tables WHERE database = currentDatabase()"
    " AND name IN ('injections', 'eval_traces', 'eval_exclusions')"
)


def statement(name: str) -> str:
    """eval_*.sql from this package; anything else from the storage contract."""
    if name.startswith("eval_"):
        return (resources.files("traceblame_faults") / "sql" / name).read_text(encoding="utf-8")
    from traceblame_storage import contract  # the store extra

    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql


def _clickhouse_ms(iso: str) -> str:
    # "2026-11-06T10:00:00.123Z" -> "2026-11-06 10:00:00.123" (DateTime64(3), UTC server).
    return iso.removesuffix("Z").replace("T", " ")


def eval_trace_params(rec: ledger.Record) -> dict[str, Any]:
    return {
        "trace_id": rec.trace_id,
        "eval_run_id": rec.eval_run_id,
        "service": rec.service,
        "source": rec.source,
        "query_ref": rec.query_ref,
        "stratum": rec.stratum,
        "golden_doc_ids": list(rec.golden_doc_ids),
        "golden_status": rec.golden_status,
        "planned_fault_kind": rec.planned_fault_kind,
        "injection_id": rec.injection_id,
        "seed": rec.seed,
    }


def injection_params(rec: ledger.Record) -> dict[str, Any]:
    assert rec.injected_at is not None  # noqa: S101 - validated before
    return {
        "trace_id": rec.trace_id,
        "injection_id": rec.injection_id,
        "fault_kind": rec.fault_kind,
        "target_component": rec.target_component,
        "params": kinds.params_json(rec.params),
        "seed": rec.seed,
        "injected_at": _clickhouse_ms(rec.injected_at),
    }


def exclusion_params(a: attempts.Abandoned) -> dict[str, Any]:
    attempts.check_abandoned(a)
    return {
        "trace_id": a.trace_id,
        "eval_run_id": a.eval_run_id,
        "service": a.service,
        "entry_id": a.entry_id,
        "attempt_id": a.attempt_id,
        "planned_fault_kind": a.planned_fault_kind,
        "injection_id": a.injection_id,
        "reason": a.reason,
        "source": a.source,
    }


def tables(client: Client) -> set[str]:
    """Which of injections, eval_traces and eval_exclusions the schema has."""
    return {str(r[0]) for r in client.query_rows(_TABLES)}


def trace_id(value: Any) -> str:
    """A FixedString(32) as the client returns it."""
    return bytes(value).decode() if isinstance(value, bytes | bytearray) else str(value)


def record(
    client: Client,
    records: Sequence[ledger.Record],
    abandoned: Sequence[attempts.Abandoned] = (),
) -> dict[str, int]:
    """The ledger into eval_traces and injections, the abandoned traces into eval_exclusions.
    Everything is validated, and every refusal happens, before the first write."""
    for rec in records:
        ledger.validate(rec)
    for a in abandoned:
        attempts.check_abandoned(a)
    both = sorted({r.trace_id for r in records} & {a.trace_id for a in abandoned})
    if both:
        raise RecordError(
            f"abandoned traces in the ledger, which would give them an eval_traces row: {both}"
        )
    found = tables(client)
    missing = {"injections", "eval_traces"} - found
    if abandoned and "eval_exclusions" not in found:
        missing.add("eval_exclusions")
    if missing:
        raise RecordError(f"no table {sorted(missing)}: run `traceblame-storage migrate` first")
    eval_write = statement("eval_traces_write.sql")
    injection_write = statement("injections_write.sql")
    for rec in records:
        client.command(eval_write, eval_trace_params(rec))
    applied = [r for r in records if ledger.status(r) == "applied"]
    for rec in applied:
        client.command(injection_write, injection_params(rec))
    return {
        "eval_traces": len(records),
        "injections": len(applied),
        "eval_exclusions": record_exclusions(client, abandoned),
    }


def record_exclusions(client: Client, abandoned: Sequence[attempts.Abandoned]) -> int:
    """Abandoned traces into eval_exclusions (ReplacingMergeTree: idempotent). Refuses one that
    has an eval_traces row."""
    if not abandoned:
        return 0
    if "eval_exclusions" not in tables(client):
        raise RecordError("no table ['eval_exclusions']: run `traceblame-storage migrate` first")
    labelled = {
        trace_id(r[0])
        for run in sorted({a.eval_run_id for a in abandoned})
        for r in client.query_rows(statement("eval_traces_read.sql"), {"eval_run_id": run})
    } & {a.trace_id for a in abandoned}
    if labelled:
        raise RecordError(f"abandoned traces with an eval_traces row: {sorted(labelled)}")
    write = statement("eval_exclusions_write.sql")
    for a in abandoned:
        client.command(write, exclusion_params(a))
    return len(abandoned)


def exclusions(client: Client, eval_run_id: str) -> list[attempts.Abandoned]:
    """The run's eval_exclusions rows; none on a schema without the table (N-1)."""
    if "eval_exclusions" not in tables(client):
        return []
    rows = client.query_rows(statement("eval_exclusions_read.sql"), {"eval_run_id": eval_run_id})
    return [attempts.Abandoned(trace_id(r[0]), *(str(v) for v in r[1:])) for r in rows]
