"""Ground truth into ClickHouse: a ledger into eval_traces (every trace) and injections (applied
faults only), through the statements of the storage contract.

- injections: contract/injections_write.sql of traceblame-storage. It inserts only when no row
  with the same (trace_id, injection_id) exists, so re-recording a ledger adds nothing.
- eval_traces: new in this schema, so its statements are not contract statements yet (code N
  runs on schema N-1). The canonical copies are tests/integration/eval_traces_*.sql; this
  package embeds them (sql/), and a contract test keeps both equal.

Needs the `store` extra (traceblame-storage). A schema without eval_traces is refused before
anything is written: run `traceblame-storage migrate` first.
"""

from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any, Protocol

from traceblame_faults import kinds, ledger


class RecordError(RuntimeError):
    pass


class Client(Protocol):
    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object: ...

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]: ...


_TABLES = (
    "SELECT name FROM system.tables WHERE database = currentDatabase()"
    " AND name IN ('injections', 'eval_traces')"
)


def statement(name: str) -> str:
    """eval_traces_*.sql from this package; anything else from the storage contract."""
    if name.startswith("eval_traces_"):
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


def record(client: Client, records: Sequence[ledger.Record]) -> dict[str, int]:
    for rec in records:
        ledger.validate(rec)
    tables = {r[0] for r in client.query_rows(_TABLES)}
    missing = {"injections", "eval_traces"} - tables
    if missing:
        raise RecordError(f"no table {sorted(missing)}: run `traceblame-storage migrate` first")
    eval_write = statement("eval_traces_write.sql")
    injection_write = statement("injections_write.sql")
    for rec in records:
        client.command(eval_write, eval_trace_params(rec))
    applied = [r for r in records if ledger.status(r) == "applied"]
    for rec in applied:
        client.command(injection_write, injection_params(rec))
    return {"eval_traces": len(records), "injections": len(applied)}
