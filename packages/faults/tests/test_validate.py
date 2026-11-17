"""validate(): one record, trace and injection per entry; no orphans; abandoned traces listed and
never labelled; zero checked = fail (#70)."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from traceblame_faults import attempts, ids, ledger, plan, record, runner, scenario, validate

RUN = "faults-20261117-s19-abc1234"


def _trace(n: int) -> str:
    return f"{n:032x}"


def _entries() -> list[plan.Entry]:
    queries = [
        plan.Query(f"Contract {i}__Governing Law", "agentic-rag", "cuad", "answerable", "mapped")
        for i in range(2)
    ]
    return plan.plan(
        seed=19,
        eval_run_id=RUN,
        app="agentic-rag",
        queries=queries,
        counts={"tool_failure": 2},
        clean=2,
        scenarios=scenario.load_all(),
    )


def _record(entry: plan.Entry, trace_id: str) -> ledger.Record:
    base = ledger.Record(
        trace_id=trace_id,
        eval_run_id=RUN,
        service=entry.app,
        source="cuad",
        query_ref=entry.query_ref,
        stratum="answerable",
        golden_doc_ids=(),
        golden_status="unmapped",
        planned_fault_kind=entry.kind,
        injection_id="",
        fault_kind="",
        target_component="",
    )
    if not entry.kind:
        return base
    return replace(
        base,
        injection_id=entry.injection_id,
        fault_kind=entry.kind,
        target_component="tool",
        params=dict(entry.params),
        injected_at="2026-11-17T10:00:00.000Z",
    )


class Run:
    """A run directory driven through the real runner, and what a store would hold for it."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        self.entries = _entries()
        self.stored: dict[str, validate.StoredTrace] = {}
        self.n = 0

    def open(self) -> runner.Attempt:
        return runner.open_attempt(self.dir, self.entries, eval_run_id=RUN, provenance={"c": 1})

    def span(self, entry: plan.Entry, *, marker: bool = True) -> str:
        """A stored trace of the entry, started now."""
        self.n += 1
        trace_id = _trace(self.n)
        markers: frozenset[str] = (
            frozenset({entry.injection_id}) if entry.kind and marker else frozenset()
        )
        now = attempts.seconds(attempts.stamp())
        self.stored[trace_id] = validate.StoredTrace(trace_id, now, markers, 3)
        return trace_id

    def complete(self, attempt: runner.Attempt, entries: list[plan.Entry] | None = None) -> None:
        def run_one(entry: plan.Entry, log: runner.EntryLog) -> ledger.Record:
            trace_id = self.span(entry)
            log.trace(entry, trace_id)
            return _record(entry, trace_id)

        runner.run_entries(
            attempt.entry_log(), self.entries if entries is None else entries, run_one
        )

    def check(self, **overrides: Any) -> validate.Result:
        records = ledger.read(self.dir / runner.LEDGER)
        events = attempts.read(self.dir / attempts.FILE)
        args: dict[str, Any] = {
            "eval_run_id": RUN,
            "entries": self.entries,
            "records": records,
            "events": events,
            # What `record` writes: one eval_traces row per record, one injection per applied.
            "eval_rows": [(r.trace_id, r.planned_fault_kind, r.injection_id) for r in records],
            "injections": [
                (r.trace_id, r.injection_id) for r in records if ledger.status(r) == "applied"
            ],
            "exclusions": [a.trace_id for a in attempts.abandoned(events)],
            "stored": list(self.stored.values()),
        }
        return validate.validate(**(args | overrides))


@pytest.fixture
def run(tmp_path: Path) -> Run:
    return Run(tmp_path)


def test_a_run_without_a_kill_passes_and_counts_what_it_checked(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    result = run.check()
    assert result.problems == []
    s = result.summary
    assert (s["entries"], s["records"], s["eval_traces_rows"], s["stored_traces"]) == (4, 4, 4, 4)
    assert s["injection_ids_checked"] == 2
    assert (s["killed"], s["abandoned"], s["orphans"], s["duplicate_injections"]) == (0, [], [], 0)


def test_a_killed_and_resumed_run_passes_with_its_abandoned_trace_listed(run: Run) -> None:
    faulted = next(e for e in run.entries if e.kind)
    first = run.open()
    run.complete(first, run.entries[:1])
    log = first.entry_log()
    target = log.remaining(run.entries)[0]
    assert target.kind  # the entry in flight is a faulted one: its marker is on two traces
    assert target.entry_id == faulted.entry_id or target.kind
    log.start(target)
    partial = run.span(target)
    log.trace(target, partial)
    import os

    os.close(first.lock_fd)
    second = run.open()
    run.complete(second)
    second.end()
    result = run.check()
    assert result.problems == []
    s = result.summary
    assert s["killed"] == 1
    (gone,) = s["abandoned"]
    assert (gone["trace_id"], gone["reason"], gone["stored_spans"]) == (partial, "killed", 3)
    assert s["abandoned_by_kind"] == {target.kind: 1}
    assert s["orphans"] == []
    # The injection id is on two stored traces, and exactly one of them is completed.
    marked = [t for t in run.stored.values() if target.injection_id in t.markers]
    assert len(marked) == 2
    assert partial not in {r.trace_id for r in ledger.read(run.dir / runner.LEDGER)}


def test_an_orphan_of_an_entry_in_flight_is_swept_and_any_other_fails(run: Run) -> None:
    import os

    first = run.open()
    log = first.entry_log()
    target = run.entries[0]
    log.start(target)
    unseen = run.span(target, marker=False)  # the kill came before its trace id was logged
    os.close(first.lock_fd)
    second = run.open()
    run.complete(second)
    second.end()
    stray = _trace(999)
    run.stored[stray] = validate.StoredTrace(
        stray, attempts.seconds(attempts.stamp()) + 3600, frozenset(), 1
    )
    result = run.check()
    (swept,) = result.sweep
    assert (swept.trace_id, swept.entry_id, swept.source, swept.reason) == (
        unseen, target.entry_id, "orphan_sweep", "killed",
    )  # fmt: skip
    assert result.summary["orphans"] == [stray]
    assert [p for p in result.problems if "orphan" in p] == [
        f"{stray}: an orphan: stored with the run's marker, neither completed nor abandoned,"
        " and not attributable to an entry in flight"
    ]
    assert len(result.problems) == 1


def test_an_orphan_with_the_marker_of_an_entry_in_flight_is_swept_by_its_marker(run: Run) -> None:
    import os

    faulted = next(e for e in run.entries if e.kind)
    first = run.open()
    run.complete(first, run.entries[: run.entries.index(faulted)])
    first.entry_log().start(faulted)
    os.close(first.lock_fd)
    second = run.open()
    run.complete(second)
    second.end()
    late = _trace(777)  # stored long after its window, but it carries the entry's marker
    run.stored[late] = validate.StoredTrace(
        late, attempts.seconds(attempts.stamp()) + 3600, frozenset({faulted.injection_id}), 2
    )
    result = run.check()
    assert [a.trace_id for a in result.sweep] == [late]
    assert result.problems == []


def test_an_orphan_with_a_marker_of_a_completed_entry_is_not_swept(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    faulted = next(e for e in run.entries if e.kind)
    twin = _trace(555)
    run.stored[twin] = validate.StoredTrace(
        twin, attempts.seconds(attempts.stamp()), frozenset({faulted.injection_id}), 2
    )
    result = run.check()
    assert result.sweep == []
    assert result.summary["orphans"] == [twin]


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"entries": []}, "the plan has no entries"),
        ({"stored": []}, "no stored trace"),
        ({"eval_rows": []}, "no eval_traces row"),
    ],
)
def test_zero_checked_fails(run: Run, override: dict[str, Any], match: str) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    assert any(match in p for p in run.check(**override).problems)


def test_an_unfinished_run_names_every_entry_without_a_record(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt, run.entries[:1])
    attempt.end()
    problems = run.check().problems
    assert len([p for p in problems if "no ledger record" in p]) == 3


def test_a_missing_a_different_and_an_extra_eval_traces_row_fail(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    records = ledger.read(run.dir / runner.LEDGER)
    rows = [(r.trace_id, r.planned_fault_kind, r.injection_id) for r in records]
    assert any("0 eval_traces rows" in p for p in run.check(eval_rows=rows[1:]).problems)
    wrong = [(rows[0][0], "malformed_output", rows[0][2]), *rows[1:]]
    assert any("a different one" in p for p in run.check(eval_rows=wrong).problems)
    extra = [*rows, (_trace(4242), "", "")]
    assert any("without a ledger record" in p for p in run.check(eval_rows=extra).problems)


def test_an_injection_on_another_trace_or_a_missing_one_fails(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    records = [r for r in ledger.read(run.dir / runner.LEDGER) if ledger.status(r) == "applied"]
    good = [(r.trace_id, r.injection_id) for r in records]
    twice = [*good, (_trace(4242), records[0].injection_id)]
    result = run.check(injections=twice)
    assert any("on 2 traces (1 on its own)" in p for p in result.problems)
    assert result.summary["duplicate_injections"] == 1
    assert any("on 0 traces" in p for p in run.check(injections=good[1:]).problems)


def test_an_abandoned_trace_is_never_labelled(run: Run) -> None:
    import os

    first = run.open()
    log = first.entry_log()
    log.start(run.entries[0])
    partial = run.span(run.entries[0])
    log.trace(run.entries[0], partial)
    os.close(first.lock_fd)
    second = run.open()
    run.complete(second)
    second.end()
    records = ledger.read(run.dir / runner.LEDGER)
    rows = [(r.trace_id, r.planned_fault_kind, r.injection_id) for r in records]
    labelled = run.check(eval_rows=[*rows, (partial, "", "")]).problems
    assert f"{partial}: in eval_exclusions and in eval_traces" in labelled
    assert f"{partial}: an eval_traces row without a ledger record" in labelled
    unrecorded = run.check(exclusions=[]).problems
    assert unrecorded == [f"{partial}: abandoned, not in eval_exclusions (run record)"]
    stranger = run.check(exclusions=[partial, _trace(31337)]).problems
    assert stranger == [f"{_trace(31337)}: in eval_exclusions without an abandoned event"]


def test_two_records_for_an_entry_and_a_record_without_an_entry_fail(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    records = ledger.read(run.dir / runner.LEDGER)
    twice = [*records, replace(records[0], trace_id=_trace(4242))]
    assert any("2 ledger records" in p for p in run.check(records=twice).problems)
    bare = [replace(records[0], entry_id=None), *records[1:]]
    assert any("without an entry_id" in p for p in run.check(records=bare).problems)
    other = replace(
        records[0],
        query_ref="Other__X",
        entry_id=ids.entry_id("agentic-rag", records[0].planned_fault_kind, "Other__X"),
        injection_id="",
        fault_kind="",
        target_component="",
        params={},
        injected_at=None,
        planned_fault_kind="",
    )
    other = replace(other, entry_id=ids.entry_id("agentic-rag", "", "Other__X"))
    assert any("the plan does not have" in p for p in run.check(records=[*records, other]).problems)


def test_the_marker_of_a_clean_record_on_a_completed_trace_fails(run: Run) -> None:
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    clean = next(r for r in ledger.read(run.dir / runner.LEDGER) if not r.planned_fault_kind)
    run.stored[clean.trace_id] = replace(
        run.stored[clean.trace_id], markers=frozenset({"faults-x:tool_failure:0123456789ab"})
    )
    assert any("did not apply it" in p for p in run.check().problems)


def test_record_params_are_what_the_checks_compare(run: Run) -> None:
    """The stand-in rows above are the ones record() derives from a record."""
    attempt = run.open()
    run.complete(attempt)
    attempt.end()
    rec = ledger.read(run.dir / runner.LEDGER)[0]
    params = record.eval_trace_params(rec)
    assert (params["trace_id"], params["planned_fault_kind"], params["injection_id"]) == (
        rec.trace_id, rec.planned_fault_kind, rec.injection_id,
    )  # fmt: skip
