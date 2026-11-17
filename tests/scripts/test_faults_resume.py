"""A fault run survives SIGKILL (#70): a real process (faults_resume_agent.py) runs the runner
with a fake agent that makes real OpenTelemetry spans, is killed with SIGKILL at a known point,
and the run is resumed with the same run id.

After every resume: each plan entry has exactly one ledger record, injection ids are unique in
the ledger, the kill and the abandoned trace are recorded, the abandoned trace's partial spans
are in the span file and classified as abandoned (not counted, not orphans), and the validation
of section 6 passes over the ledger, the attempt log, what `record` writes and the span file.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from traceblame_faults import attempts, ledger, plan, record, runner, scenario, validate

AGENT = Path(__file__).with_name("faults_resume_agent.py")
RUN = "faults-smoke-20261117-s7-abc1234"
TIMEOUT = 30.0


class Store:
    """What `record` writes, kept in memory: the stand-in for eval_traces, injections and
    eval_exclusions next to the span file."""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {
            "eval_traces": [],
            "injections": [],
            "eval_exclusions": [],
        }

    def command(self, sql: str, parameters: Mapping[str, Any] | None = None) -> object:
        table = sql.split("INSERT INTO ", 1)[1].split()[0]
        self.rows[table].append(dict(parameters or {}))
        return None

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        if "system.tables" in sql:
            return [(t,) for t in self.rows]
        if sql == record.statement("eval_traces_read.sql"):
            return [(r["trace_id"],) for r in self.rows["eval_traces"]]
        return []


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "run"
        self.dir.mkdir()
        self.spans = tmp_path / "spans.jsonl"
        self.fifo = tmp_path / "block.fifo"
        self.ready = tmp_path / "ready"
        os.mkfifo(self.fifo)
        queries = [
            plan.Query(
                f"Contract {i}__Governing Law", "agentic-rag", "cuad", "answerable", "mapped"
            )
            for i in range(2)
        ]
        self.entries = plan.plan(
            seed=7,
            eval_run_id=RUN,
            app="agentic-rag",
            queries=queries,
            counts={"tool_failure": 2},
            clean=2,
            scenarios=scenario.load_all(),
        )
        self.plan = tmp_path / "plan.jsonl"
        self.plan.write_text(plan.dumps(self.entries), encoding="utf-8")
        self.procs: list[subprocess.Popen[str]] = []

    def start(
        self, *, block: str | None = None, child: bool = False, provenance: str | None = None
    ) -> subprocess.Popen[str]:
        argv = [sys.executable, str(AGENT), "driver", "--run-dir", str(self.dir),
                "--plan", str(self.plan), "--spans", str(self.spans)]  # fmt: skip
        if block is not None:
            argv += ["--block", block, "--ready", str(self.ready), "--fifo", str(self.fifo)]
        if child:
            argv.append("--child")
        if provenance is not None:
            argv += ["--provenance", provenance]
        proc = subprocess.Popen(  # noqa: S603 - fixed argv
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # its own process group: killpg never reaches pytest
        )
        self.procs.append(proc)
        return proc

    def finish(self, **kwargs: Any) -> tuple[int, dict[str, Any], str]:
        """An attempt that runs to its end: exit code, what it printed, its stderr."""
        proc = self.start(**kwargs)
        out, err = proc.communicate(timeout=TIMEOUT)
        return proc.returncode, (json.loads(out.splitlines()[0]) if out.strip() else {}), err

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        lines = (self.dir / attempts.FILE).read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        return [e for e in events if name is None or e["event"] == name]

    def wait(self, what: str, check: Callable[[], bool]) -> None:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")

    def wait_blocked(self) -> str:
        self.wait("the process to reach its block point", self.ready.is_file)
        point = self.ready.read_text(encoding="utf-8").strip()
        self.ready.unlink()
        return point

    def kill_group(self, proc: subprocess.Popen[str]) -> None:
        os.killpg(proc.pid, signal.SIGKILL)
        assert proc.wait(timeout=TIMEOUT) == -signal.SIGKILL

    def release(self) -> None:
        """Let a blocked process go on: give its FIFO a writer."""
        with open(self.fifo, "w", encoding="utf-8"):
            pass

    def stored(self) -> list[validate.StoredTrace]:
        """The span file as the orphan sweep reads the store: one row per trace."""
        by_trace: dict[str, list[dict[str, Any]]] = {}
        for line in self.spans.read_text(encoding="utf-8").splitlines():
            span = json.loads(line)
            assert span["resource"]["aegis.eval.run.id"] == RUN
            by_trace.setdefault(span["trace_id"], []).append(span)
        return [
            validate.StoredTrace(
                trace_id,
                min(float(s["start"]) for s in spans),
                frozenset(
                    str(s["attrs"]["aegis.fault.injection.id"])
                    for s in spans
                    if "aegis.fault.injection.id" in s["attrs"]
                ),
                len(spans),
            )
            for trace_id, spans in by_trace.items()
        ]

    def check(self) -> validate.Result:
        """Record the ledger and the abandoned traces, then the validation of section 6."""
        records = ledger.read(self.dir / runner.LEDGER)
        events = attempts.read(self.dir / attempts.FILE)
        store = Store()
        record.record(store, records, attempts.abandoned(events))
        return validate.validate(
            eval_run_id=RUN,
            entries=self.entries,
            records=records,
            events=events,
            eval_rows=[
                (r["trace_id"], r["planned_fault_kind"], r["injection_id"])
                for r in store.rows["eval_traces"]
            ],
            injections=[(r["trace_id"], r["injection_id"]) for r in store.rows["injections"]],
            exclusions=[r["trace_id"] for r in store.rows["eval_exclusions"]],
            stored=self.stored(),
        )

    def assert_complete(self) -> validate.Result:
        """Every entry has exactly one record; injection ids are unique; the validation passes."""
        records = ledger.read(self.dir / runner.LEDGER)
        assert sorted(r.entry_id or "" for r in records) == sorted(e.entry_id for e in self.entries)
        injected = [r.injection_id for r in records if r.injection_id]
        assert len(injected) == len(set(injected)) == 2
        result = self.check()
        assert result.problems == []
        assert result.summary["entries"] == len(self.entries) > 0
        assert result.summary["stored_traces"] >= len(self.entries)
        assert result.summary["orphans"] == []
        return result


@pytest.fixture
def run(tmp_path: Path) -> Iterator[Run]:
    r = Run(tmp_path)
    yield r
    # Every process group first, whether its leader lives or not: a child that outlived its
    # driver is in the driver's group and holds the driver's pipes, so waiting on those pipes
    # before the child is gone would never return.
    for proc in r.procs:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    for proc in r.procs:
        proc.communicate(timeout=TIMEOUT)


def test_sigkill_mid_trace_then_resume(run: Run) -> None:
    faulted = next(e for e in run.entries if e.kind)
    proc = run.start(block=f"mid-trace:{faulted.entry_id}")
    assert run.wait_blocked() == f"mid-trace:{faulted.entry_id}"
    (seen,) = [
        e["trace_id"] for e in run.events("entry_trace") if e["entry_id"] == faulted.entry_id
    ]
    before = run.entries.index(faulted)
    assert len(ledger.read(run.dir / runner.LEDGER)) == before
    run.kill_group(proc)

    code, printed, err = run.finish()
    assert (code, err) == (0, "")
    assert printed["remaining"] == len(run.entries) - before
    result = run.assert_complete()
    # The kill is recorded.
    (killed,) = run.events("attempt_killed")
    assert killed["status"] == "killed/unknown"
    assert killed["last_event"] == f"entry_trace {faulted.entry_id}"
    assert result.summary["killed"] == 1
    # The abandoned trace is recorded, with the trace id the first attempt logged.
    (gone,) = result.summary["abandoned"]
    assert (gone["trace_id"], gone["entry_id"], gone["reason"], gone["source"]) == (
        seen, faulted.entry_id, "killed", "attempt_log",
    )  # fmt: skip
    # Its partial spans are stored (the child span had ended, the root never did) and are
    # classified as abandoned: not counted as a completed trace, not an orphan.
    partial = [s for s in run.stored() if s.trace_id == seen]
    assert [(s.spans, sorted(s.markers)) for s in partial] == [(1, [faulted.injection_id])]
    assert gone["stored_spans"] == 1
    assert seen not in {r.trace_id for r in ledger.read(run.dir / runner.LEDGER)}
    # The injection id is on two stored traces; exactly one of them is the entry's record.
    marked = [s.trace_id for s in run.stored() if faulted.injection_id in s.markers]
    assert len(marked) == 2
    assert result.summary["duplicate_injections"] == 0


def test_sigkill_of_the_driver_alone_blocks_a_resume_until_its_child_is_gone(run: Run) -> None:
    target = run.entries[1]
    proc = run.start(block=f"mid-trace:{target.entry_id}", child=True)
    run.wait_blocked()
    os.kill(proc.pid, signal.SIGKILL)  # the driver only: its child keeps the run lock
    assert proc.wait(timeout=TIMEOUT) == -signal.SIGKILL

    code, _, err = run.finish()
    assert code == 3
    (first,) = run.events("attempt_start")
    assert f"attempt {first['attempt_id']} still running, pid {proc.pid}" in err
    assert len(run.events("attempt_start")) == 1, "a refused resume starts no attempt"

    run.release()  # the child finishes its entries and exits
    run.wait(
        "the child to finish",
        lambda: len(ledger.read(run.dir / runner.LEDGER)) == len(run.entries),
    )
    run.wait("the run lock to be free", lambda: _lock_free(run.dir))
    code, printed, err = run.finish()
    assert (code, err, printed["remaining"]) == (0, "", 0)
    result = run.assert_complete()
    # The driver never wrote its attempt_end: the attempt is recorded as killed, and nothing
    # was in flight, because the child completed every entry.
    assert [e["attempt_id"] for e in run.events("attempt_killed")] == [first["attempt_id"]]
    assert result.summary["abandoned"] == []


def _lock_free(run_dir: Path) -> bool:
    try:
        os.close(runner.lock(run_dir))
    except runner.BusyError:
        return False
    return True


def test_a_torn_last_ledger_line_is_set_aside_and_never_recorded(run: Run) -> None:
    target = run.entries[1]
    proc = run.start(block=f"mid-trace:{target.entry_id}")
    run.wait_blocked()
    (seen,) = [e["trace_id"] for e in run.events("entry_trace") if e["entry_id"] == target.entry_id]
    run.kill_group(proc)
    # The kill came in the middle of the record's write: whole JSON, no newline.
    whole = (run.dir / runner.LEDGER).read_bytes()
    first = ledger.read(run.dir / runner.LEDGER)[0]
    torn = ledger.dumps(
        replace(
            first, trace_id=seen, query_ref=target.query_ref, planned_fault_kind="", entry_id=None
        )
    ).encode()
    with (run.dir / runner.LEDGER).open("ab") as f:
        f.write(torn)
    # Nothing reads a torn ledger: `record` cannot give the torn line's trace an eval_traces row.
    with pytest.raises(ledger.LedgerError, match="torn tail"):
        run.check()

    code, _, err = run.finish()
    assert (code, err) == (0, "")
    result = run.assert_complete()
    (event,) = run.events("torn_tail")
    assert (event["offset"], event["length"]) == (len(whole), len(torn))
    assert (run.dir / event["copy"]).read_bytes() == torn
    assert result.summary["torn_tails"] == 1
    (gone,) = result.summary["abandoned"]
    assert (gone["trace_id"], gone["reason"]) == (seen, "torn_tail")


def test_sigkill_between_a_record_and_the_next_entry_start_loses_nothing(run: Run) -> None:
    first = run.entries[0]
    proc = run.start(block=f"after-record:{first.entry_id}")
    run.wait_blocked()
    run.kill_group(proc)
    code, printed, err = run.finish()
    assert (code, err, printed["remaining"]) == (0, "", len(run.entries) - 1)
    result = run.assert_complete()
    assert result.summary["killed"] == 1
    assert (result.summary["abandoned"], result.summary["lost_entries"]) == ([], 0)


def test_sigkill_before_the_first_entry_runs_every_entry_on_resume(run: Run) -> None:
    proc = run.start(block="before-first")
    assert run.wait_blocked() == "before-first:"
    run.kill_group(proc)
    assert not (run.dir / runner.LEDGER).exists()
    code, printed, err = run.finish()
    assert (code, err, printed["remaining"]) == (0, "", len(run.entries))
    result = run.assert_complete()
    assert (result.summary["killed"], result.summary["abandoned"]) == (1, [])


def test_a_resume_with_another_provenance_is_refused(run: Run) -> None:
    proc = run.start(block=f"mid-trace:{run.entries[1].entry_id}")
    run.wait_blocked()
    run.kill_group(proc)
    before = (run.dir / runner.LEDGER).read_bytes()
    code, _, err = run.finish(provenance='{"commit": "def5678"}')
    assert code == 3
    assert 'commit: "abc1234" -> "def5678"' in err
    assert "start a new run" in err
    assert (run.dir / runner.LEDGER).read_bytes() == before
    assert len(run.events("resume_refused")) == 1
    assert len(run.events("attempt_start")) == 1
    # The first provenance still resumes.
    code, _, err = run.finish()
    assert (code, err) == (0, "")
    run.assert_complete()


def test_a_second_resume_after_a_clean_finish_has_nothing_to_run(run: Run) -> None:
    code, printed, _ = run.finish()
    assert (code, printed["remaining"]) == (0, len(run.entries))
    before = (run.dir / runner.LEDGER).read_bytes()
    spans = run.spans.read_bytes()
    code, printed, err = run.finish()
    assert (code, err, printed["remaining"]) == (0, "", 0)
    assert (run.dir / runner.LEDGER).read_bytes() == before
    assert run.spans.read_bytes() == spans, "no entry ran again"
    result = run.assert_complete()
    assert [a["status"] for a in result.summary["attempts"]] == ["completed", "completed"]
    assert result.summary["killed"] == 0


def test_nothing_unlinks_or_rewrites_the_ledger_of_a_run() -> None:
    """The #14 drivers began their run phase with an unlink of ledger.jsonl. Nothing does any
    more: the ledger and the attempt log are only ever appended to, and only a resume cuts a
    torn tail off (appendlog.repair, the one ftruncate)."""
    root = Path(__file__).resolve().parents[2]
    sources = [
        *sorted((root / "tools" / "faults").glob("*.py")),
        *sorted((root / "packages" / "faults" / "src" / "traceblame_faults").glob("*.py")),
    ]
    assert len(sources) > 20, "the drivers and the library were found"
    unlinking = [p.name for p in sources if ".unlink(" in p.read_text(encoding="utf-8")]
    assert unlinking == []
    truncating = [p.name for p in sources if "os.ftruncate(" in p.read_text(encoding="utf-8")]
    assert truncating == ["appendlog.py"]
    assert not [p.name for p in sources if "os.truncate(" in p.read_text(encoding="utf-8")]
    writing = [
        p.name
        for p in sources
        if "LEDGER).write_" in p.read_text(encoding="utf-8")
        or '"ledger.jsonl").write_' in p.read_text(encoding="utf-8")
    ]
    assert writing == []
