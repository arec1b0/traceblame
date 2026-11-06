"""Leakage by value on real faulted traces (#15, rule 9): the trace views of the newest fault run
in the kind cluster hold no label-like string. Read-only, through a port-forward, under the
shared kind lock (taken here, waited for at most KIND_LOCK_WAIT seconds); skipped with the
reason when there is no cluster, no run or no lock.

Run: uv run --frozen pytest -m integration -s tests/scripts/test_detectors_kind_scan.py
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.integration

LOCK = Path(os.environ.get("KIND_LOCK", "/tmp/traceblame-kind.lock"))  # noqa: S108 - the shared lock
LOCK_WAIT_S = float(os.environ.get("KIND_LOCK_WAIT", "300"))
LABEL_LIKE = re.compile(r"inject|fault|aegis", re.IGNORECASE)
NOT_LABELS = {"profile_version": re.compile(r"^aegis-telemetry/\d+$")}
ROOT = Path(__file__).resolve().parents[2]
KUBECTL = ROOT / ".tools" / "bin" / "kubectl"
# The traces of one eval run (parameter run), and what the scan reads about them.
TRACES = (
    "SELECT toString(trace_id), argMax(assembler_version, revision) FROM trace_summary "
    "WHERE trace_id IN (SELECT trace_id FROM eval_traces FINAL WHERE eval_run_id = {run:String}) "
    "GROUP BY trace_id ORDER BY trace_id"
)
INJECTED_TEXT_KEYS = (
    "SELECT k, count() FROM spans ARRAY JOIN mapKeys(attrs) AS k, mapValues(attrs) AS v "
    "WHERE trace_id IN (SELECT trace_id FROM eval_traces FINAL WHERE eval_run_id = {run:String}) "
    "AND positionCaseInsensitive(v, 'injected') > 0 GROUP BY k"
)
FAULT_MARKED = (
    "SELECT count() FROM spans WHERE attrs['aegis.fault.injection.id'] != '' "
    "AND trace_id IN (SELECT trace_id FROM eval_traces FINAL WHERE eval_run_id = {run:String})"
)
RUN_MARKED = (
    "SELECT count() FROM spans WHERE resource_attrs['aegis.eval.run.id'] != '' "
    "AND trace_id IN (SELECT trace_id FROM eval_traces FINAL WHERE eval_run_id = {run:String})"
)


def _cluster_reason() -> str | None:
    kubectl = str(KUBECTL) if KUBECTL.is_file() else shutil.which("kubectl")
    if kubectl is None:
        return "no kubectl"
    probe = subprocess.run(  # noqa: S603 - fixed argv
        [
            kubectl,
            "--context",
            "kind-traceblame",
            "-n",
            "observability",
            "get",
            "svc",
            "clickhouse-traceblame",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return None if probe.returncode == 0 else "no kind-traceblame cluster with ClickHouse"


@pytest.fixture
def kind_lock() -> Iterator[None]:
    reason = _cluster_reason()
    if reason:
        pytest.skip(reason)
    with LOCK.open("a") as fh:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    pytest.skip(f"{LOCK} held by another session for {LOCK_WAIT_S:.0f} s")
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def test_real_faulted_traces_leave_no_label_in_the_trace_view(kind_lock: None) -> None:
    from faultslib import clickhouse_client  # tools/faults: kind ClickHouse over a port-forward

    from traceblame_detectors.policy import DECLARABLE
    from traceblame_detectors.sources.clickhouse import ClickHouseTraceSource

    with clickhouse_client() as ch:
        runs = ch.query_rows(
            "SELECT eval_run_id, countIf(planned_fault_kind != '') FROM eval_traces FINAL "
            "WHERE eval_run_id LIKE 'faults-%' GROUP BY eval_run_id ORDER BY max(recorded_at) DESC"
        )
        runs = [r for r in runs if r[1] > 0]
        if not runs:
            pytest.skip("no fault run with faulted traces in eval_traces")
        run_id = str(runs[0][0])
        run = {"run": run_id}
        traces = ch.query_rows(TRACES, run)
        # Where the injected error text and the markers are stored at all (outside the view).
        stored: dict[str, Any] = {
            "attr_keys_with_injected_text": dict(ch.query_rows(INJECTED_TEXT_KEYS, run)),
            "spans_with_fault_marker": ch.query_rows(FAULT_MARKED, run)[0][0],
            "spans_with_run_marker": ch.query_rows(RUN_MARKED, run)[0][0],
        }
        source = ClickHouseTraceSource(ch)
        scanned_traces = scanned_values = 0
        allowed: Counter[str] = Counter()
        hits: dict[str, list[str]] = {}
        for trace_id, assembler_version in traces:
            view = source.load(str(trace_id), str(assembler_version), DECLARABLE)
            if view is None:
                continue
            scanned_traces += 1
            trace = view.canonical()
            fields: list[tuple[str, Any]] = [(k, v) for k, v in trace.items() if k != "spans"]
            for span in trace["spans"]:
                for k, v in span.items():
                    if k == "attrs":
                        values = list(cast("dict[str, Any]", v).values())
                    elif isinstance(v, list):
                        values = list(cast("list[Any]", v))
                    else:
                        values = [v]
                    fields.extend((k, x) for x in values)
            for name, value in fields:
                if not isinstance(value, str):
                    continue
                scanned_values += 1
                if LABEL_LIKE.search(value):
                    pattern = NOT_LABELS.get(name)
                    if pattern and pattern.fullmatch(value):
                        allowed[name] += 1
                    else:
                        hits.setdefault(name, []).append(value)
    report = {
        "run_id": run_id,
        "traces_scanned": scanned_traces,
        "string_values_scanned": scanned_values,
        "allowed_matches": dict(allowed),
        "hits": {k: sorted(set(v))[:5] for k, v in hits.items()},
        "stored_outside_the_view": stored,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    assert scanned_traces > 0
    assert scanned_values > 0
    assert stored["spans_with_fault_marker"] > 0, (
        "the scan only means something if markers are stored"
    )
    assert hits == {}
