"""The SQL the detectors run: reads of `spans` and `trace_summary` from the storage contract,
and their own statements on findings, detector_runs and detector_params, embedded here until
those columns and tables are on main (then they move into the contract; code N runs on schema
N-1). tests/contract/test_detectors_sql_copies.py keeps the copies equal to tests/integration/.
"""

from importlib import resources

from traceblame_storage import contract


def statement(name: str) -> str:
    if name.startswith("detectors_"):
        return (resources.files("traceblame_detectors") / "sql" / name).read_text(encoding="utf-8")
    (sql,) = [s for n, s in contract.packaged() if n == name]
    return sql
