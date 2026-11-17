"""tools/faults/report.py: what the validation report re-derives from the run's ledger and plan
(modes, counts, host load), the D1 rule for golden_context_removed, and the content-check line.
Ledgers, plans and check.json entries here are test doubles, never eval data."""

import hashlib
from pathlib import Path
from typing import Any

import faultslib
import provenance
import pytest
import report  # tools/faults, on pytest's pythonpath as at the driver's run time

from traceblame_faults import ids, ledger, plan

RUN = "faults-20261106-s7-abc1234"
DIGEST = "sha256:" + "a" * 64
KIND = "golden_context_removed"
SCENARIO = "golden_context_removed-agentic-rag"
COMMON: dict[str, Any] = {
    "scenario": SCENARIO,
    "scenario_version": 1,
    "faults_version": "0.1.0",
    "top_k": 5,
}


def _trace_id(n: int) -> str:
    return f"{n:032x}"


def _record(
    n: int, ref: str, kind: str, applied: dict[str, Any] | None, load: float | None
) -> ledger.Record:
    common: dict[str, Any] = {
        "trace_id": _trace_id(n),
        "eval_run_id": RUN,
        "service": "agentic-rag",
        "source": "cuad",
        "query_ref": ref,
        "stratum": "answerable",
        "golden_doc_ids": ("cuad-0123456789ab-3",),
        "golden_status": "mapped",
        "planned_fault_kind": kind,
        "seed": 7,
        "bundle_digest": DIGEST,
        "host_load1": load,
    }
    if applied is None:
        return ledger.Record(
            **common, injection_id="", fault_kind="", target_component="", params={}
        )
    return ledger.Record(
        **common,
        injection_id=ids.injection_id(RUN, kind, ref),
        fault_kind=kind,
        target_component="retrieval",
        params=COMMON | applied | {"baseline_digest": DIGEST},
        injected_at="2026-11-06T10:00:00.123Z",
    )


def _run_dir(tmp_path: Path) -> Path:
    records = [
        _record(0, "Contract A__Law", "", None, 3.5),
        _record(
            1, "Contract A__Law", KIND, {"mode": "distractor", "distractor_doc": "cuad-x1"}, 7.25
        ),
        _record(2, "Contract B__Term", KIND, {"mode": "remove", "removed_n": 2}, 9.0),
        _record(3, "Contract C__Cap", KIND, None, None),  # remove, nothing to remove
    ]
    for r in records:
        ledger.append(tmp_path / "ledger.jsonl", r)
    entries = [
        plan.Entry(r.query_ref, "agentic-rag", KIND, SCENARIO, COMMON | {"mode": m})
        for r, m in zip(records[1:], ("distractor", "remove", "remove"), strict=True)
    ]
    (tmp_path / "plan.jsonl").write_text(plan.dumps(entries), encoding="utf-8")
    return tmp_path


def _check() -> dict[str, Any]:
    def t(n: int, kind: str, status: str, clean_hit: bool | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"trace_id": _trace_id(n), "query": f"q{n}", "kind": kind}
        out["status"] = status
        if status == "applied":
            out["signal"] = {
                "moved": True,
                "golden_in_topk": [clean_hit, False],
                "max_score": [0.2, -0.2],
            }
        return out

    return {
        "per_trace": [
            t(0, "clean", "clean"),
            t(1, KIND, "applied", clean_hit=False),
            t(2, KIND, "applied", clean_hit=True),
            t(3, KIND, "not_reached"),
        ],
        "per_kind": {
            "clean": {"applied": 0, "not_reached": 0},
            KIND: {"applied": 2, "not_reached": 1},
        },
    }


def test_the_ledger_view_rederives_modes_loads_and_counts(tmp_path: Path) -> None:
    view = report.ledger_view(_run_dir(tmp_path))
    assert view["records"] == 4
    assert view["modes"] == {
        _trace_id(0): None,
        _trace_id(1): "distractor",
        _trace_id(2): "remove",
        _trace_id(3): "remove",  # not reached: the planned mode
    }
    assert view["host_load1"][_trace_id(1)] == 7.25
    assert view["host_load1"][_trace_id(3)] is None
    assert view["counts"] == {
        "clean": {"clean": 1, "applied": 0, "not_reached": 0},
        KIND: {"clean": 0, "applied": 2, "not_reached": 1},
    }
    assert report.ledger_problems(_check(), view) == []
    wrong = _check()
    wrong["per_kind"][KIND]["applied"] = 3
    assert report.ledger_problems(wrong, view) == [
        f"{KIND}: check.json and the ledger disagree on applied / not_reached"
    ]


def test_retrieval_rows_apply_the_d1_rule(tmp_path: Path) -> None:
    view = report.ledger_view(_run_dir(tmp_path))
    rows = report.retrieval_rows(_check(), view["modes"])
    assert [(r["query"], r["status"], r["mode"], r["counted"]) for r in rows] == [
        ("q1", "applied", "distractor", True),  # no golden in the clean top-k, but a distractor
        ("q2", "applied", "remove", True),  # golden was in the clean top-k
        ("q3", "not_reached", "remove", False),
    ]
    # KIND has loads 7.25 and 9.0 (the not_reached trace has none): the true median.
    assert report.loads_by_kind(_check(), view) == {"clean": 3.5, KIND: 8.12}


def test_the_content_line_names_needles_and_everything_scanned() -> None:
    cc = {
        "needles": {"queries": 4, "contract_chunks": 120, "windows": 9000},
        "scanned": {
            "rows": {"spans": 366, "eval_traces": 28},
            "rows_total": 394,
            "exported_spans": 366,
            "files": {"otlp.json": 1, "ledger.jsonl": 28},
            "id_fields_excluded": {"fields": ["query_ref", "ref"], "values": 56},
        },
        "leaking": 0,
    }
    line = report.content_line("agentic-rag", cc)
    assert line.startswith("content check: 0 leaking. Needles: 4 sampled CUAD questions")
    assert "394 rows (eval_traces 28, spans 366)" in line
    assert "the 366 exported spans" in line
    assert "ledger.jsonl (28 lines)" in line
    assert "otlp.json (1 line)" in line
    assert "left out: 56 values of query_ref, ref that are exactly one of the run's own" in line
    assert "Any other value in those fields is scanned" in line
    assert report.content_line("agentic-rag", {"checked": 4, "leaking": 0}).startswith(
        "content check (spans only)"
    )


def test_the_report_takes_its_date_from_run_json() -> None:
    run = {"run_id": "faults-x", "created_at": "2026-11-05T23:59:59Z"}
    assert report.run_date(run) == "2026-11-05"


def _malformed_check(moved: bool, kind: str = "malformed_output") -> dict[str, Any]:
    return {
        "problems": [],
        "per_trace": [
            {
                "trace_id": _trace_id(1),
                "query": "q1",
                "kind": kind,
                "status": "applied",
                "signal": {
                    "moved": moved,
                    "parse_ok": [["true"], ["true" if not moved else "false"]],
                },
            }
        ],
        "per_kind": {},
    }


def test_a_marked_malformed_output_with_parse_ok_true_fails_the_report() -> None:
    # check.json written before check_run reported it: the report re-derives the problem.
    problems = report.signal_problems(_malformed_check(moved=False))
    assert problems == [f"{_trace_id(1)} malformed_output: applied, but its signal did not move"]
    md = report.markdown(_minimal(problems=problems))
    assert "All checks passed." not in md
    assert "malformed_output: applied, but its signal did not move" in md


def test_a_moved_signal_and_step_memory_loss_misses_are_not_per_trace_problems() -> None:
    assert report.signal_problems(_malformed_check(moved=True)) == []
    assert report.signal_problems(_malformed_check(False, kind="step_memory_loss")) == []


def test_the_report_never_says_all_passed_with_a_problem() -> None:
    assert "All checks passed." in report.markdown(_minimal(problems=[]))
    assert "All checks passed." not in report.markdown(_minimal(problems=["x"]))


def _minimal(problems: list[str]) -> dict[str, Any]:
    return {
        "date": "2026-11-06",
        "apps": {},
        "retrieval": [],
        "loop_gate": report.loop_gate(
            {"per_kind": {}}, {"loop": {"hit_rate_min": 0.6, "repeat_min": 3}}
        ),
        "projection": report.projection({}),
        "d1_components": report.components(),
        "blockers": [],
        "problems": problems,
    }


def test_the_markdown_states_the_rule_and_the_empty_blockers(tmp_path: Path) -> None:
    view = report.ledger_view(_run_dir(tmp_path))
    md = report.markdown(
        {
            "date": "2026-11-06",
            "apps": {},
            "retrieval": report.retrieval_rows(_check(), view["modes"]),
            "loop_gate": report.loop_gate(
                {"per_kind": {}}, {"loop": {"hit_rate_min": 0.6, "repeat_min": 3}}
            ),
            "projection": report.projection({}),
            "d1_components": report.components(),
            "blockers": [],
            "problems": [],
        }
    )
    assert "| q1 | distractor | False | False | 0.2 -> -0.2 | True | True |" in md
    assert "| q3 | remove | None | None | None -> None | None | False |" in md
    assert (
        "Re-derived from the ledger: 2 applied (distractor 1, remove 1), 1 not_reached;"
        " 2 counted for D1." in md
    )
    assert "Queries with no golden chunk in the clean top-k: 1; `q1` ran `distractor`." in md
    assert "None recorded." in md


def test_the_preload_lines_name_both_digests_and_what_changed() -> None:
    pre = {
        "run_sha256": "a" * 64,
        "run_commit_matches": True,
        "committed_sha256": "b" * 64,
        "commits_since": ["38dd2b8 build(faults): lint the Node preload with eslint (#14)"],
        "lines_added_removed": [5, 5],
    }
    used, changed = report.preload_lines(pre)
    assert used.startswith(f"- preload used by this run: sha256 `{'a' * 64}`")
    assert used.endswith("(equal to the file at the run's commit)")
    assert f"sha256 `{'b' * 64}`" in changed
    assert (
        "`38dd2b8 build(faults): lint the Node preload with eslint (#14)` (5 lines added" in changed
    )
    assert "D1 runs with the committed preload" in changed
    same = pre | {"committed_sha256": "a" * 64}
    assert report.preload_lines(same)[1] == "- the committed preload is the same file"
    assert "**not**" in report.preload_lines(pre | {"run_commit_matches": False})[0]
    assert report.preload_lines(None) == []


def test_the_preload_provenance_reads_git(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {
        "run1:" + str(report.PRELOAD.relative_to(report.ROOT)): b"old",
        "HEAD:" + str(report.PRELOAD.relative_to(report.ROOT)): b"new",
    }

    def git(*argv: str) -> bytes:
        if argv[0] == "show":
            return files[argv[1]]
        if argv[0] == "diff":
            return b"5\t5\tpackages/faults/preload/faults-preload.mjs\n"
        return b"38dd2b8 build(faults): lint\n"

    monkeypatch.setattr(report, "_git", git)
    old = hashlib.sha256(b"old").hexdigest()
    pre = report.preload_provenance("run1", old)
    assert pre == {
        "run_sha256": old,
        "run_commit_matches": True,
        "committed_sha256": hashlib.sha256(b"new").hexdigest(),
        "commits_since": ["38dd2b8 build(faults): lint"],
        "lines_added_removed": [5, 5],
    }


def test_load_medians_are_true_medians_shared_with_check_json() -> None:
    check = {
        "per_trace": [
            {"trace_id": _trace_id(n), "kind": KIND, "status": "applied"} for n in range(4)
        ]
    }
    view = {"host_load1": {_trace_id(n): float(n + 1) for n in range(4)}}  # 1, 2, 3, 4
    assert report.loads_by_kind(check, view) == {KIND: 2.5}
    # The same helper as check_run's host_load1_median.
    assert faultslib.median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert faultslib.median([3.0, 1.0, 2.0]) == 2.0
    assert faultslib.median([]) is None


# provenance (#65) --------------------------------------------------------------------------


def _snapshot(at: str, extra_migration: bool = False) -> dict[str, Any]:
    migrations = {
        "clickhouse": [
            {
                "version": "0001",
                "name": "spans",
                "phase": "expand",
                "sha256": "ec39fa3ba092",
                "applied_at": "2026-11-05T13:23:49Z",
            }
        ],
        "postgres": [
            {
                "version": "0001",
                "name": "assembler_partitions",
                "phase": "expand",
                "sha256": "8ae1a3710e21",
                "applied_at": "2026-11-04T15:31:58Z",
            }
        ],
    }
    if extra_migration:
        migrations["postgres"].append(
            {**migrations["postgres"][0], "version": "0002", "name": "assembler_replicas"}
        )
    roles = ("otel-agent", "otel-gateway", "trace-assembler", "clickhouse", "nats")
    images = {
        role: [
            {
                "pod": f"{role}-a",
                "container": "main",
                "image": "app:tag",
                "image_id": f"docker.io/library/import-2026-11-06@sha256:{i:064x}",
            }
        ]
        for i, role in enumerate(roles, 1)
    }
    return {"at": at, "images": images, "migrations": migrations, "errors": {}}


def _phase(extra_migration: bool = False) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "start": _snapshot("2026-11-06T09:00:00Z"),
        "end": _snapshot("2026-11-06T09:30:00Z", extra_migration),
    }
    rec["problems"] = provenance.phase_problems(rec)
    return rec


EVAL_ID = "sha256:" + "e" * 64


CONFIG_ID = "sha256:" + "c" * 64


def _state(
    *, recorded: bool, app: str = "runbook-agent", node_config: str = CONFIG_ID
) -> dict[str, Any]:
    state: dict[str, Any] = {"build": {"eval_image_id": EVAL_ID}}
    if recorded:
        run = _phase()
        run["eval"] = {
            "image": "runbook-agent:abc-faults",
            "built_image_id": EVAL_ID,
            "built_config_id": CONFIG_ID,
        }
        run["eval_pods"] = [
            {
                "label": "end",
                "at": "x",
                "pods": [
                    {
                        "pod": "runbook-agent-x",
                        "container": "runbook-agent",
                        "image": "runbook-agent:abc-faults",
                        "image_id": "docker.io/library/import-2026-11-06@sha256:" + "7" * 64,
                        "node": "traceblame-worker",
                        "node_config_ids": [node_config],
                    }
                ],
            }
        ]
        state |= {
            "dirty": False,
            "app_dirty": False,
            "provenance": {"run": run, "record": _phase(), "check": _phase()},
        }
    return state


def test_a_run_before_the_provenance_says_not_recorded_for_every_new_field() -> None:
    state = _state(recorded=False)
    lines = "\n".join(
        report.provenance_lines("runbook-agent", state, report.provenance_info(state))
    )
    assert report.provenance_info(state) == {
        "dirty": "not recorded (predates #65)",
        "app_dirty": "not recorded (predates #65)",
        "phases": "not recorded (predates #65)",
    }
    assert "dirty flag at the build: not recorded (predates #65)" in lines
    assert "applied migrations (ClickHouse and Postgres `schema_migrations`): not recorded" in lines
    assert (
        "pipeline image IDs (otel agent and gateway, trace-assembler, ClickHouse, NATS): not"
        in lines
    )
    assert (
        f"eval pod image ID: not recorded (predates #65); built eval_image_id `{EVAL_ID}`" in lines
    )
    assert provenance.run_problems(state.get("provenance"), "runbook-agent") == []


def test_a_recorded_run_shows_its_images_migrations_and_flags_in_the_markdown() -> None:
    state = _state(recorded=True)
    lines = "\n".join(
        report.provenance_lines("runbook-agent", state, report.provenance_info(state))
    )
    assert "dirty flag at the build: traceblame false, app false" in lines
    assert "| run | 2026-11-06T09:00:00Z | 2026-11-06T09:30:00Z | ClickHouse 1 (`" in lines
    assert "| none | 0 |" in lines
    assert "ClickHouse at the end of the run phase: 0001 spans (expand, ec39fa3ba092," in lines
    assert "Postgres: 0001 assembler_partitions (expand, 8ae1a3710e21," in lines
    assert (
        f"| otel-gateway | 1 | main | `docker.io/library/import-2026-11-06@sha256:{2:064x}` |"
        in lines
    )
    assert (
        f"config digest built `{CONFIG_ID}` against the config digest the pods' nodes held" in lines
    )
    assert f"`{CONFIG_ID}`: equal." in lines
    assert "kept the imageID `docker.io/library/import-2026-11-06@sha256:" + "7" * 64 in lines
    assert f"the build's `eval_image_id` is `{EVAL_ID}`" in lines
    assert "not comparable" not in lines


def test_the_report_says_not_equal_when_a_node_holds_another_config_digest() -> None:
    state = _state(recorded=True, node_config="sha256:" + "d" * 64)
    lines = "\n".join(
        report.provenance_lines("runbook-agent", state, report.provenance_info(state))
    )
    assert f"`{'d' * 64 and 'sha256:' + 'd' * 64}`: **not equal**." in lines
    problems = provenance.run_problems(state["provenance"], "runbook-agent")
    assert any("holds config digest dddddddddddd" in p for p in problems)


def test_a_change_within_a_phase_is_visible_in_the_table_and_a_problem_in_the_report() -> None:
    state = _state(recorded=True)
    state["provenance"]["check"] = _phase(extra_migration=True)
    lines = "\n".join(
        report.provenance_lines("runbook-agent", state, report.provenance_info(state))
    )
    assert (
        "postgres: applied migrations changed during the phase: added 0002 assembler_replicas"
        in lines
    )
    problems = provenance.run_problems(state["provenance"], "runbook-agent")
    assert any(p.startswith("check: postgres: applied migrations changed") for p in problems)


def test_a_dirty_checkout_in_a_state_is_a_problem() -> None:
    assert report.dirty_problems({"dirty": False, "app_dirty": False}) == []
    assert report.dirty_problems({}) == []  # a run that predates the flag
    assert report.dirty_problems({"dirty": True, "app_dirty": True}) == [
        "the traceblame checkout was dirty",
        "the app checkout was dirty",
    ]


# A resumed run (#70): what the report reads, and what it says about the resume ----------------

RESUMED_RUN = "faults-d1-20261117-s19-abc1234"


def _resumed(tmp_path: Path) -> tuple[Path, list[plan.Entry], str]:
    """An app's run directory after a kill and a resume, made with the real runner: every entry
    completed once, and the trace in flight at the kill abandoned."""
    import os

    from traceblame_faults import runner, scenario

    queries = [
        plan.Query(
            f"Contract {i}__Law",
            "agentic-rag",
            "cuad",
            "answerable",
            "mapped",
            split="test" if i else "calib",
        )
        for i in range(2)
    ]
    entries = plan.plan(
        seed=19,
        eval_run_id=RESUMED_RUN,
        app="agentic-rag",
        queries=queries,
        counts={"tool_failure": 2},
        clean=2,
        scenarios=scenario.load_all(),
    )
    d = tmp_path / "agentic-rag"
    d.mkdir()
    (d / "plan.jsonl").write_text(plan.dumps(entries), encoding="utf-8")
    (d / "queries.jsonl").write_text(plan.dump_queries(queries), encoding="utf-8")

    def rec(entry: plan.Entry, n: int) -> ledger.Record:
        return ledger.Record(
            trace_id=_trace_id(n),
            eval_run_id=RESUMED_RUN,
            service="agentic-rag",
            source="cuad",
            query_ref=entry.query_ref,
            stratum="answerable",
            golden_doc_ids=(),
            golden_status="unmapped",
            planned_fault_kind=entry.kind,
            injection_id="",
            fault_kind="",
            target_component="",
            host_load1=1.5,
        )

    first = runner.open_attempt(d, entries, eval_run_id=RESUMED_RUN, provenance={"c": 1})
    log = first.entry_log()
    log.start(entries[0])
    log.record(entries[0], rec(entries[0], 0))
    log.start(entries[1])
    abandoned = _trace_id(99)
    log.trace(entries[1], abandoned)
    os.close(first.lock_fd)  # killed
    second = runner.open_attempt(d, entries, eval_run_id=RESUMED_RUN, provenance={"c": 1})
    runner.run_entries(second.entry_log(), entries, lambda e, _: rec(e, 10 + entries.index(e)))
    second.end()
    return d, entries, abandoned


def _resume_summary(d: Path, entries: list[plan.Entry]) -> dict[str, Any]:
    from traceblame_faults import attempts, validate

    records = ledger.read(d / "ledger.jsonl")
    events = attempts.read(d / attempts.FILE)
    gone = [a.trace_id for a in attempts.abandoned(events)]
    stored = [
        validate.StoredTrace(t, 0.0, frozenset(), 2)
        for t in [*gone, *(r.trace_id for r in records)]
    ]
    result = validate.validate(
        eval_run_id=RESUMED_RUN,
        entries=entries,
        records=records,
        events=events,
        eval_rows=[(r.trace_id, r.planned_fault_kind, r.injection_id) for r in records],
        injections=[],
        exclusions=gone,
        stored=stored,
    )
    return result.summary | {"problems": result.problems}


def test_reader_the_report_input_has_no_abandoned_trace(tmp_path: Path) -> None:
    d, entries, abandoned = _resumed(tmp_path)
    records = ledger.read(d / "ledger.jsonl")
    # What the report reads of a trace: the ledger view and the split view, both from the ledger.
    view = report.ledger_view(d)
    assert view["records"] == len(entries) == 4
    assert abandoned not in view["host_load1"]
    assert abandoned not in view["modes"]
    assert sum(sum(c.values()) for c in view["counts"].values()) == 4
    split = report.split_view(d, records)
    assert split is not None
    assert sum(n["calib"] + n["test"] for n in split["traces"].values()) == 4
    # It is in the resume section, and nowhere else.
    resume = _resume_summary(d, entries)
    assert [a["trace_id"] for a in resume["abandoned"]] == [abandoned]
    lines = report.resume_lines(resume)
    assert sum(abandoned in line for line in lines) == 1
    (row,) = [line for line in lines if abandoned in line]
    assert row.startswith(f"| `{abandoned}` | {entries[1].entry_id} |")
    assert "| killed | attempt_log | 2 |" in row


def test_the_resume_section_shows_the_kill_and_no_duplicates_or_orphans(tmp_path: Path) -> None:
    d, entries, _ = _resumed(tmp_path)
    resume = _resume_summary(d, entries)
    assert resume["problems"] == []
    text = "\n".join(report.resume_lines(resume))
    assert "- checked: 4 plan entries, 4 ledger records, 4 `eval_traces` rows, 5 traces" in text
    assert (
        "- attempts: 2, killed: 1; entries in flight that ran again: 1; torn ledger tails: 0"
        in text
    )
    assert "never in `eval_traces` or `injections`): 1; by kind:" in text
    assert "- injection ids with `injections` rows on more than one trace: 0" in text
    assert "- orphans (stored, neither completed nor abandoned): 0" in text
    assert "| killed |" in text
    assert "| completed |" in text
    assert report.resume_lines(None) == []
    assert report.resume_problems({"resume": resume}) == []
    assert report.resume_problems({}) == []


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"entries": 0}, "nothing checked (entries = 0)"),
        ({"stored_traces": 0}, "nothing checked (stored_traces = 0)"),
        ({"eval_traces_rows": 0}, "nothing checked (eval_traces_rows = 0)"),
        ({"orphans": ["f" * 32]}, "1 orphan traces"),
        ({"duplicate_injections": 2}, "2 injection ids with injections rows on more than one"),
        ({"problems": ["x: an orphan"]}, "resume: x: an orphan"),
    ],
)
def test_a_resume_check_that_checked_nothing_or_found_something_is_a_problem(
    tmp_path: Path, change: dict[str, Any], match: str
) -> None:
    d, entries, _ = _resumed(tmp_path)
    resume = _resume_summary(d, entries) | change
    assert any(match in p for p in report.resume_problems({"resume": resume}))


def test_the_split_section_counts_queries_and_traces_per_side(tmp_path: Path) -> None:
    d, _, _ = _resumed(tmp_path)
    view = report.split_view(d, ledger.read(d / "ledger.jsonl"))
    assert view == {
        "queries_planned": {"calib": 1, "test": 1},
        "queries_with_traces": {"calib": 1, "test": 1},
        "traces": {"clean": {"calib": 1, "test": 1}, "tool_failure": {"calib": 1, "test": 1}},
    }
    lock = {  # the shape of tools/faults/plans/<plan>.queries.lock.json
        "counts": {"agentic-rag": {"queries": 110}},
        "agentic_rag": {
            "cuad_contracts": {"total": 62, "calib": 38, "test": 41, "both_sides": 17},
            "excluded_listed": 12,
            "excluded_removed": 2,
        },
        "runbook_agent": {"tasks": 90, "distinct_tuples": 90, "excluded_tuples": 11},
    }
    md = "\n".join(
        report.split_lines({"plan": "d1", "split": {"agentic-rag": view}, "split_lock": lock})
    )
    assert "| agentic-rag | 1 | 1 | 1 | 1 |" in md
    assert "| agentic-rag | tool_failure | 1 | 1 |" in md
    assert "#19 reports query-cluster bootstrap 95% CIs" in md
    assert "110 questions come from 62 contracts" in md
    assert "and **17 on both**" in md
    assert "90 tasks with 90 distinct (scenario," in md
    assert report.split_lines({"plan": "smoke", "split": {}}) == []
    assert report.split_lock("smoke") is None


def test_a_plan_without_a_split_has_no_split_view(tmp_path: Path) -> None:
    d, _, _ = _resumed(tmp_path)
    text = (d / "queries.jsonl").read_text(encoding="utf-8")
    (d / "queries.jsonl").write_text(
        text.replace('"split":"calib"', '"split":""').replace('"split":"test"', '"split":""'),
        encoding="utf-8",
    )
    assert report.split_view(d, ledger.read(d / "ledger.jsonl")) is None


def test_the_report_file_and_title_name_the_plan() -> None:
    validation = {"run_id": RUN, "created_at": "2026-11-06T10:00:00Z"}
    assert report.stem(validation) == "faults-kind-2026-11-06"
    d1_run = {"run_id": RESUMED_RUN, "created_at": "2026-11-17T10:00:00Z", "plan": {"name": "d1"}}
    assert report.stem(d1_run) == "faults-d1-kind-2026-11-17"
    again = d1_run | {"run_id": RESUMED_RUN + "-2"}
    assert report.stem(again) == "faults-d1-kind-2026-11-17-2"
    digits = d1_run | {"run_id": "faults-d1-20261117-s19-1234567"}
    assert report.stem(digits) == "faults-d1-kind-2026-11-17", "a commit of digits is no counter"
    assert report.title({"date": "2026-11-06"}) == (
        "# Fault injection through kind: validation run (2026-11-06)"
    )
    assert "`smoke` run" in report.title({"date": "2026-11-17", "plan": "smoke"})


def test_the_loop_gate_is_the_validation_runs() -> None:
    gate = report.loop_gate({"per_kind": {}}, {"loop": {"hit_rate_min": 0.6, "repeat_min": 3}})
    assert not gate["passed"]
    base = _minimal(problems=[])
    gated = report.markdown(base | {"loop_gate": gate | {"gated": True}})
    assert "**Failed**" in gated
    reported = report.markdown(base | {"loop_gate": gate | {"gated": False}})
    assert "Reported, not gated" in reported
    assert "**Failed**" not in reported
