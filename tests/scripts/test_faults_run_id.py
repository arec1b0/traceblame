"""The fault run's id is computed once and written to .tools/faults/<run>/run.json; every phase
and the report resolve the run from there, never from the date or the commit again (PR #64,
Cursor finding). The work directory is a tmp_path; no kind, no eval data."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import faultslib  # tools/faults, on pytest's pythonpath as at the driver's run time
import pytest
import report

COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
BEFORE = datetime(2026, 11, 5, 23, 59, 59, tzinfo=UTC)
AFTER = datetime(2026, 11, 6, 0, 0, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(faultslib, "WORK", tmp_path)
    return tmp_path


def test_a_new_run_is_written_once_with_its_seed_and_commit(work: Path) -> None:
    run_id = faultslib.new_run(seed=11, commit=COMMIT, now=BEFORE)
    assert run_id == "faults-20261105-s11-abcdef0"
    assert json.loads((work / run_id / "run.json").read_text(encoding="utf-8")) == {
        "run_id": run_id,
        "seed": 11,
        "traceblame_commit": COMMIT,
        "created_at": "2026-11-05T23:59:59Z",
    }


def test_phases_straddling_utc_midnight_with_another_seed_resolve_one_directory(
    work: Path,
) -> None:
    # prepare (agentic-rag) just before midnight, build (runbook-agent) and the report after.
    run_id = faultslib.ensure_run(None, seed=11, commit=COMMIT, now=BEFORE)
    later = faultslib.ensure_run(run_id, seed=11, commit=COMMIT, now=AFTER)
    assert later == run_id
    assert faultslib.run_dir(later, "runbook-agent") == work / run_id / "runbook-agent"
    run = faultslib.open_run(run_id)
    assert run["created_at"] == "2026-11-05T23:59:59Z"  # not rewritten after midnight
    assert report.run_date(run) == "2026-11-05"
    assert sorted(p.name for p in work.iterdir()) == [run_id]


def test_an_explicit_run_id_is_kept_as_given(work: Path) -> None:
    run_id = faultslib.ensure_run("faults-mine", seed=7, commit=COMMIT, now=AFTER)
    assert run_id == "faults-mine"
    assert faultslib.open_run("faults-mine")["seed"] == 7


@pytest.mark.parametrize(
    ("seed", "commit", "match"),
    [(7, COMMIT, "seed"), (11, "f" * 40, "commit")],
)
def test_a_phase_with_another_seed_or_commit_is_refused(seed: int, commit: str, match: str) -> None:
    run_id = faultslib.new_run(seed=11, commit=COMMIT, now=BEFORE)
    with pytest.raises(faultslib.RunError, match=match):
        faultslib.ensure_run(run_id, seed=seed, commit=commit, now=AFTER)


def test_an_unknown_run_is_refused() -> None:
    with pytest.raises(faultslib.RunError, match=r"no run\.json"):
        faultslib.open_run("faults-20261106-s7-nothere")


def test_a_run_from_before_run_json_is_adopted_from_its_states(work: Path) -> None:
    run_id = "faults-20261105-s7-b1b6b60"
    for app, stamp in (("agentic-rag", "prepared_at"), ("runbook-agent", "built_at")):
        d = work / run_id / app
        d.mkdir(parents=True)
        state = {"run_id": run_id, "seed": 7, "traceblame_commit": COMMIT}
        state[stamp] = "2026-11-05T16:00:00Z" if app == "agentic-rag" else "2026-11-05T17:00:00Z"
        (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
    run = faultslib.adopt_run(run_id)
    assert run == {
        "run_id": run_id,
        "seed": 7,
        "traceblame_commit": COMMIT,
        "created_at": "2026-11-05T16:00:00Z",
    }
    assert faultslib.open_run(run_id) == run


def test_adopting_refuses_states_of_different_runs(work: Path) -> None:
    run_id = "faults-x"
    for app, seed in (("agentic-rag", 7), ("runbook-agent", 8)):
        d = work / run_id / app
        d.mkdir(parents=True)
        state = {"run_id": run_id, "seed": seed, "traceblame_commit": COMMIT,
                 "prepared_at": "2026-11-05T16:00:00Z"}  # fmt: skip
        (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(faultslib.RunError, match="seed"):
        faultslib.adopt_run(run_id)


def test_faults_validate_passes_one_run_id_to_every_phase() -> None:
    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["make", "-n", "--no-print-directory", "faults-validate", "RUN_ID=faults-given"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "date -u" not in out
    assert "rev-parse" not in out
    for phase in ("run_agentic_rag.py prepare", "run_runbook_agent.py build", "report.py"):
        lines = [line for line in out.splitlines() if phase in line]
        assert lines, phase
        assert all("faults-given" in line for line in lines), phase


def test_faults_validate_computes_the_id_once_without_run_id() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "Makefile").read_text(encoding="utf-8")
    recipe = text.split("\nfaults-validate:", 1)[1].split("\n\n", 1)[0]
    assert recipe.count("run_id.py new") == 1
    assert "date" not in recipe
    assert "rev-parse" not in recipe


def test_every_phase_resolves_its_state_through_run_json(work: Path) -> None:
    run_id = faultslib.new_run(seed=11, commit=COMMIT, now=BEFORE)
    d = faultslib.run_dir(run_id, "agentic-rag")
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"run_id": run_id, "seed": 11}), encoding="utf-8")
    assert faultslib.load_state(d)["run_id"] == run_id
    (d / "state.json").write_text(json.dumps({"run_id": run_id, "seed": 7}), encoding="utf-8")
    with pytest.raises(faultslib.RunError, match="not of run"):
        faultslib.load_state(d)
    orphan = work / "faults-orphan" / "agentic-rag"
    orphan.mkdir(parents=True)
    (orphan / "state.json").write_text('{"run_id": "faults-orphan", "seed": 7}', encoding="utf-8")
    with pytest.raises(faultslib.RunError, match=r"no run\.json"):
        faultslib.load_state(orphan)
