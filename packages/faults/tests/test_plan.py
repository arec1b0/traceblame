"""plan(): same seed, same fault choice, parameters and injection point, byte for byte."""

import pytest

from traceblame_faults import ids, kinds, plan, scenario

RUN = "faults-20261106-s7-abc1234"


def _queries(n: int, *, app: str = "agentic-rag") -> list[plan.Query]:
    out: list[plan.Query] = []
    for i in range(n):
        answerable = i % 2 == 0
        out.append(
            plan.Query(
                ref=f"contract {i}__Some Category",
                app=app,
                source="cuad",
                stratum="answerable" if answerable else "unanswerable",
                golden_status="mapped" if answerable else "none",
            )
        )
    return out


PY_COUNTS = {
    "golden_context_removed": 3,
    "tool_failure": 4,
    "malformed_output": 2,
    "output_budget_exhausted": 2,
    "step_memory_loss": 2,
    "prompt_variant": 2,
}


def _plan(seed: int = 7, queries: list[plan.Query] | None = None) -> list[plan.Entry]:
    return plan.plan(
        seed=seed,
        eval_run_id=RUN,
        app="agentic-rag",
        queries=queries if queries is not None else _queries(12),
        counts=PY_COUNTS,
        clean=5,
        scenarios=scenario.load_all(),
    )


def test_same_seed_gives_a_byte_identical_plan() -> None:
    a = plan.dumps(_plan())
    assert a == plan.dumps(_plan())
    assert a == plan.dumps(_plan(queries=list(reversed(_queries(12)))))
    assert plan.dumps(_plan(seed=8)) != a


def test_counts_clean_and_eligibility() -> None:
    entries = _plan()
    by_kind: dict[str, int] = {}
    for e in entries:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    assert by_kind == {"": 5} | PY_COUNTS
    golden = [e for e in entries if e.kind == "golden_context_removed"]
    refs = {q.ref: q for q in _queries(12)}
    assert all(refs[e.query_ref].golden_status == "mapped" for e in golden)


def test_entries_carry_valid_params_ids_and_the_seed() -> None:
    for e in _plan():
        assert e.seed == 7
        if e.kind:
            kinds.check_params(e.kind, e.params)
            assert e.injection_id == ids.injection_id(RUN, e.kind, e.query_ref)
            assert e.scenario == e.params["scenario"]
        else:
            assert (e.params, e.injection_id, e.scenario) == ({}, "", "")


def test_a_query_runs_at_most_once_per_kind_and_entries_are_grouped_by_query() -> None:
    entries = _plan()
    pairs = [(e.query_ref, e.kind) for e in entries]
    assert len(pairs) == len(set(pairs))
    seen: list[str] = []
    for e in entries:
        if not seen or seen[-1] != e.query_ref:
            assert e.query_ref not in seen, "a query's entries are contiguous"
            seen.append(e.query_ref)


def test_the_validation_shape_pairs_every_kind_with_a_clean_run_of_the_same_query() -> None:
    queries = [q for q in _queries(8) if q.stratum == "answerable"]
    entries = plan.plan(
        seed=7,
        eval_run_id=RUN,
        app="agentic-rag",
        queries=queries,
        counts=dict.fromkeys(PY_COUNTS, len(queries)),
        clean=len(queries),
        scenarios=scenario.load_all(),
    )
    for q in queries:
        assert sorted(e.kind for e in entries if e.query_ref == q.ref) == sorted(["", *PY_COUNTS])


def test_too_few_eligible_queries_is_an_error() -> None:
    with pytest.raises(plan.PlanError, match="golden_context_removed"):
        plan.plan(
            seed=7,
            eval_run_id=RUN,
            app="agentic-rag",
            queries=_queries(4),
            counts={"golden_context_removed": 3},
            clean=0,
            scenarios=scenario.load_all(),
        )


def test_a_kind_the_app_does_not_carry_is_an_error() -> None:
    with pytest.raises(plan.PlanError, match="tool_failure"):
        plan.plan(
            seed=7,
            eval_run_id=RUN,
            app="runbook-agent",
            queries=_queries(4, app="runbook-agent"),
            counts={"tool_failure": 1},
            clean=0,
            scenarios=scenario.load_all(),
        )


def test_dumps_and_loads_round_trip() -> None:
    entries = _plan()
    assert plan.loads(plan.dumps(entries)) == entries


def test_queries_round_trip_and_reject_other_apps() -> None:
    qs = _queries(3)
    assert plan.load_queries(plan.dump_queries(qs)) == qs
    with pytest.raises(plan.PlanError, match="app"):
        plan.plan(
            seed=7,
            eval_run_id=RUN,
            app="agentic-rag",
            queries=_queries(2, app="runbook-agent"),
            counts={},
            clean=1,
            scenarios=scenario.load_all(),
        )


def test_exclusive_gives_every_query_one_entry_at_most() -> None:
    queries = _queries(12, app="runbook-agent")
    queries = [q.with_(stratum="oom", golden_status="none", source="runbook") for q in queries]
    entries = plan.plan(
        seed=7,
        eval_run_id=RUN,
        app="runbook-agent",
        queries=queries,
        counts={"malformed_output": 3, "prompt_variant": 3},
        clean=3,
        scenarios=scenario.load_all(),
        exclusive=True,
    )
    assert len(entries) == 9
    assert len({e.query_ref for e in entries}) == 9
    with pytest.raises(plan.PlanError, match="prompt_variant"):
        plan.plan(
            seed=7,
            eval_run_id=RUN,
            app="runbook-agent",
            queries=queries[:5],
            counts={"malformed_output": 3, "prompt_variant": 3},
            clean=0,
            scenarios=scenario.load_all(),
            exclusive=True,
        )
