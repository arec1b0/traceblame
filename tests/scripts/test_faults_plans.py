"""The committed plan files and query lists (#70): tools/faults/plans/.

d1.yaml is the #19 composition and the report reads it; the D1 split is committed, by source
query, half per app and balanced per fault kind; D1 and the #14 validation run share no query,
by ref and (runbook tasks) by (scenario, parameters) tuple. Every check counts what it checked
and fails on zero.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import capture_agentic_rag as rag
import cuad
import d1_queries
import faultslib  # tools/faults, on pytest's pythonpath as at the driver's run time
import kindlib
import pytest
import report
import run_runbook_agent
import scenarios

from traceblame_faults import ids, kinds, planfile, querylist

ROOT = Path(__file__).resolve().parents[2]
PLANS = ROOT / "tools" / "faults" / "plans"
RAG, RUNBOOK = "agentic-rag", "runbook-agent"
VALIDATION_REPORT = ROOT / "evals" / "reports" / "faults-kind-2026-11-05.json"
# D1 as #19 composes it (the comment "From #14 (fault injector)" on the issue).
ISSUE_19 = {
    (RAG, "golden_context_removed"): 75,
    (RAG, "tool_failure"): 75,
    (RAG, "output_budget_exhausted"): 75,
    (RAG, "step_memory_loss"): 75,
    (RAG, "malformed_output"): 50,
    (RAG, "prompt_variant"): 50,
    (RAG, "clean"): 110,
    (RUNBOOK, "malformed_output"): 25,
    (RUNBOOK, "prompt_variant"): 25,
    (RUNBOOK, "clean"): 40,
}


def _rows(name: str) -> list[querylist.Row]:
    return querylist.loads((PLANS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def d1() -> list[querylist.Row]:
    return _rows("d1.queries.jsonl")


@pytest.fixture(scope="module")
def validation() -> list[querylist.Row]:
    return _rows("validation.queries.jsonl")


@pytest.fixture(scope="module")
def lock() -> dict[str, Any]:
    return json.loads((PLANS / "d1.queries.lock.json").read_text(encoding="utf-8"))


# The plan files -------------------------------------------------------------------------------


def test_d1_is_the_issue_19_composition() -> None:
    composition = planfile.composition(faultslib.config("d1"))
    assert composition == ISSUE_19
    faulted = sum(n for (_, kind), n in composition.items() if kind != "clean")
    clean = sum(n for (_, kind), n in composition.items() if kind == "clean")
    assert (faulted, clean) == (450, 150)


def test_the_report_reads_its_d1_table_from_the_plan_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert report.d1() == ISSUE_19
    by_component = report.components()
    assert by_component["orchestration"] == 150
    assert {by_component[c] for c in ("retrieval", "tool", "model", "prompt")} == {75}
    # Another plan file gives another table: the report holds no copy of its own.
    text = (PLANS / "d1.yaml").read_text(encoding="utf-8").replace("clean: 110", "clean: 111")
    (tmp_path / "d1.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(faultslib, "PLANS", tmp_path)
    assert report.d1()[RAG, "clean"] == 111


def test_the_d1_seed_is_not_the_validation_seed() -> None:
    d1_cfg, validation_cfg = faultslib.config("d1"), faultslib.config(faultslib.VALIDATION)
    assert validation_cfg["seed"] == 7
    assert d1_cfg["seed"] != validation_cfg["seed"]
    assert d1_cfg["split"]["max_kind_deviation"] == 2
    assert d1_cfg["exclude"] == "validation.queries.jsonl"


def test_the_smoke_plan_runs_every_kind_once_from_the_validation_pool() -> None:
    cfg = faultslib.config("smoke")
    composition = planfile.composition(cfg)
    rag_kinds = {k for k, v in kinds.KINDS.items() if RAG in v.apps}
    assert {k for (app, k), n in composition.items() if app == RAG and n == 1} == rag_kinds | {
        "clean"
    }
    assert sum(composition.values()) == 10
    # Validation's seed and CUAD sampling: its queries are in the pool D1 excludes.
    validation_cfg = faultslib.config(faultslib.VALIDATION)
    assert cfg["seed"] == validation_cfg["seed"]
    for key in ("n", "ratio"):
        assert cfg["agentic_rag"][key] == validation_cfg["agentic_rag"][key]
    assert cfg["runbook_agent"]["scenarios"] == ["latency"], "an oom task waits 11 min"
    assert "split" not in cfg


def test_a_plan_name_resolves_to_its_committed_file_only() -> None:
    assert faultslib.plan_path("d1") == PLANS / "d1.yaml"
    assert (
        faultslib.plan_path(faultslib.VALIDATION) == ROOT / "tools" / "faults" / "validation.yaml"
    )
    for bad in ("../d1", "D1", "nope"):
        with pytest.raises(SystemExit):
            faultslib.plan_path(bad)


# Disjoint from validation -----------------------------------------------------------------------


@pytest.mark.parametrize("app", [RAG, RUNBOOK])
def test_d1_and_validation_share_no_query_ref(
    app: str, d1: list[querylist.Row], validation: list[querylist.Row]
) -> None:
    ours = {r.query_ref for r in querylist.of_app(d1, app)}
    theirs = {r.query_ref for r in querylist.of_app(validation, app)}
    print(f"{app}: {len(ours)} D1 refs checked against {len(theirs)} validation refs")
    assert len(ours) == {RAG: 110, RUNBOOK: 90}[app]
    assert len(theirs) == 12
    assert ours & theirs == set()


def test_d1_and_validation_share_no_runbook_tuple_and_d1_tuples_are_distinct(
    d1: list[querylist.Row], validation: list[querylist.Row]
) -> None:
    ours = [scenarios.ref_tuple(r.query_ref) for r in querylist.of_app(d1, RUNBOOK)]
    theirs = {scenarios.ref_tuple(r.query_ref) for r in querylist.of_app(validation, RUNBOOK)}
    print(f"{len(ours)} D1 tuples checked against {len(theirs)} validation tuples")
    assert len(ours) == 90
    assert len(theirs) > 0
    assert len(set(ours)) == len(ours), "every D1 task has its own (scenario, parameters) tuple"
    assert set(ours) & theirs == set()
    # Refs alone would not show it: the seeded plan with D1's seed repeats tuples, and some of
    # them are validation's.
    seeded = [
        scenarios.tuple_key(t.scenario, t.params)
        for t in scenarios.plan(19, 45)
        if t.scenario in ("latency", "oom")
    ]
    assert len(set(seeded)) < len(seeded)
    assert set(seeded) & theirs


def test_the_validation_list_holds_every_query_the_validation_run_ran(
    validation: list[querylist.Row],
) -> None:
    ran = json.loads(VALIDATION_REPORT.read_text(encoding="utf-8"))["apps"]
    checked = 0
    for app, expected in ((RAG, 4), (RUNBOOK, 9)):
        hashes = {t["query"] for t in ran[app]["per_trace"]}
        listed = {ids.query_hash(r.query_ref) for r in querylist.of_app(validation, app)}
        assert len(hashes) == expected
        assert hashes <= listed, app
        checked += len(hashes)
    print(f"{checked} query hashes of the committed validation report found in the list")
    assert checked == 13


def test_the_validation_lists_runbook_tasks_are_the_validation_plans() -> None:
    cfg = faultslib.config(faultslib.VALIDATION)
    tasks = run_runbook_agent._tasks(cfg)  # pyright: ignore[reportPrivateUsage]
    listed = {r.query_ref for r in querylist.of_app(_rows("validation.queries.jsonl"), RUNBOOK)}
    assert listed == {f"{t.name}@s7" for t in tasks}
    assert len(listed) == 12


# The split ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("app", [RAG, RUNBOOK])
def test_each_app_is_split_in_half_and_every_kind_is_within_two_of_half_on_the_test_side(
    app: str, d1: list[querylist.Row]
) -> None:
    rows = querylist.of_app(d1, app)
    sides = Counter(r.split for r in rows)
    assert sides == {"calib": len(rows) // 2, "test": len(rows) // 2}
    per_kind: dict[str, Counter[str]] = {}
    for r in rows:
        for kind in r.kinds:
            per_kind.setdefault(kind, Counter())[r.split] += 1
    planned = {k: n for (a, k), n in ISSUE_19.items() if a == app}
    assert {k: sum(c.values()) for k, c in per_kind.items()} == planned
    for kind, c in per_kind.items():
        assert abs(c["test"] - planned[kind] / 2) <= 2, (app, kind, dict(c))
    print(
        f"{app}: {len(per_kind)} kinds checked: " + str({k: c["test"] for k, c in per_kind.items()})
    )
    assert len(per_kind) == len(planned) > 0


def test_every_trace_of_a_query_is_on_one_side(d1: list[querylist.Row]) -> None:
    """A query is listed once, with one side, so its clean trace and every faulted trace of it
    (up to seven for a CUAD question) are on that side."""
    keys = [(r.app, r.query_ref) for r in d1]
    assert len(keys) == len(set(keys)) == 200
    assert {r.split for r in d1} == {"calib", "test"}
    assert max(len(r.kinds) for r in querylist.of_app(d1, RAG)) > 1
    assert {len(r.kinds) for r in querylist.of_app(d1, RUNBOOK)} == {1}


def test_runbook_tasks_are_grouped_by_their_tuple_not_by_their_name(
    d1: list[querylist.Row],
) -> None:
    side_of: dict[str, set[str]] = {}
    for r in querylist.of_app(d1, RUNBOOK):
        side_of.setdefault(scenarios.ref_tuple(r.query_ref), set()).add(r.split)
    assert len(side_of) == 90
    assert all(len(sides) == 1 for sides in side_of.values())


def test_the_runbook_half_of_the_list_is_what_the_driver_computes(d1: list[querylist.Row]) -> None:
    """Pure (no archive, no image): build recomputes the same rows and refuses a difference."""
    _, queries, rows = run_runbook_agent.build_plan(faultslib.config("d1"), "plan")
    assert querylist.verify(d1, rows, RUNBOOK) == 90
    assert {q.ref: q.split for q in queries} == querylist.sides(d1, RUNBOOK)
    flipped = [querylist.Row(r.app, r.query_ref, "calib", r.kinds) for r in rows]
    with pytest.raises(querylist.QueryListError, match="with another split"):
        querylist.verify(d1, flipped, RUNBOOK)


# The lock ----------------------------------------------------------------------------------------


def test_the_lists_hold_ids_only_and_match_their_lock(
    d1: list[querylist.Row], lock: dict[str, Any]
) -> None:
    for name in ("d1.queries.jsonl", "validation.queries.jsonl"):
        text = (PLANS / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            assert sorted(json.loads(line)) == ["app", "kinds", "query_ref", "split"]
        assert lock["files"][name] == d1_queries._sha256(text)  # pyright: ignore[reportPrivateUsage]
    assert lock["plan_sha256"] == planfile.sha256(PLANS / "d1.yaml")
    assert lock["validation_plan_sha256"] == planfile.sha256(faultslib.CONFIG)
    assert lock["cuad"] == {
        "commit": cuad.CUAD_COMMIT,
        "archive_sha256": cuad.CUAD_SHA256,
        "split_member": cuad.SPLIT_MEMBER,
    }
    for key in ("agentic_rag_commit", "generator_commit"):
        assert len(lock[key]) == 40
    assert lock["counts"][RAG]["by_split"] == {"calib": 55, "test": 55}
    assert lock["counts"][RUNBOOK]["by_split"] == {"calib": 45, "test": 45}
    assert lock["runbook_agent"] == {"tasks": 90, "distinct_tuples": 90, "excluded_tuples": 11}
    contracts = lock["agentic_rag"]["cuad_contracts"]
    assert contracts["calib"] + contracts["test"] - contracts["both_sides"] == contracts["total"]
    assert contracts["both_sides"] > 0, "reported, not grouped"
    assert len(d1) == sum(lock["counts"][app]["queries"] for app in (RAG, RUNBOOK))
    assert report.split_lock("d1") == lock, "the report reads this lock"


def test_the_cuad_half_comes_from_the_pinned_archive_without_validations_candidates(
    d1: list[querylist.Row], validation: list[querylist.Row]
) -> None:
    """With the pinned archive cached: the validation list is what validation's sampling gives,
    and every D1 question is an answerable one of D1's own sample that validation's is not."""
    archive = rag.CACHE / "cuad" / "data.zip"
    if not archive.is_file():
        pytest.skip(f"no CUAD archive at {archive} (make faults-agentic-rag or d1_queries.py)")
    assert querylist.dumps(d1_queries.validation_rows(archive)) == (
        PLANS / "validation.queries.jsonl"
    ).read_text(encoding="utf-8")
    cfg = faultslib.config("d1")["agentic_rag"]
    sample = cuad.sample(
        cuad.load_test_split(archive), n=int(cfg["n"]), seed=19, ratio=float(cfg["ratio"])
    )
    pool = {i.qid for i in sample if i.answerable}
    theirs = {r.query_ref for r in querylist.of_app(validation, RAG)}
    ours = {r.query_ref for r in querylist.of_app(d1, RAG)}
    assert len(pool & theirs) > 0, "the seeds alone do not keep the two apart"
    assert ours <= pool - theirs
    print(f"{len(ours)} D1 questions checked against a pool of {len(pool)}, {len(pool & theirs)}"
          " of them validation candidates")  # fmt: skip
    assert len(ours) == 110


def test_the_lists_regenerate_byte_identically(lock: dict[str, Any]) -> None:
    """With the pinned archive, the agentic-rag checkout at the lock's commit and the capture
    environment: tools/faults/d1_queries.py check, in process (about 20 s)."""
    app_dir = ROOT.parent / "agentic-rag"
    archive = rag.CACHE / "cuad" / "data.zip"
    uv = ROOT / ".tools" / "bin" / "uv"  # the pinned one, never whichever uv is on PATH
    missing = [
        str(p)
        for p in (archive, app_dir / ".git", rag.VENV / "bin" / "python", uv)
        if not p.exists()
    ]
    if missing:
        pytest.skip(f"needs {missing}: run d1_queries.py check where they are")
    at = kindlib.git(app_dir, "rev-parse", "HEAD")
    if at != lock["agentic_rag_commit"] or kindlib.is_dirty(app_dir):
        pytest.skip(f"agentic-rag is at {at[:12]}, the list was generated at the lock's commit")
    compared, different = d1_queries.compare(
        d1_queries.compute("d1", app_dir, str(uv), committed=False)
    )
    print(f"{compared} queries regenerated and compared")
    assert different == []
    assert compared == 224
