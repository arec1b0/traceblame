"""The runbook-agent capture's seeded task plan and workloads (#13)."""

import importlib.util
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    path = ROOT / "tools" / "fixtures" / "runbook_agent" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"runbook_agent_{name}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


scenarios = _load("scenarios")

IMAGE = "python:pinned"


def test_the_plan_is_fixed_by_the_seed() -> None:
    assert scenarios.plan(7, 6) == scenarios.plan(7, 6)
    assert scenarios.plan(7, 6) != scenarios.plan(8, 6)


def test_six_per_scenario_with_two_timeouts_and_two_rejections() -> None:
    tasks = scenarios.plan(7, 6)
    assert Counter(t.scenario for t in tasks) == dict.fromkeys(scenarios.SCENARIOS, 6)
    assert Counter(t.approval for t in tasks) == {None: 12, "approve": 8, "reject": 2, "timeout": 2}
    assert all(
        (t.approval is None) == (t.scenario not in scenarios.APPROVAL_SCENARIOS) for t in tasks
    )
    assert [t.order for t in tasks] == list(range(24))
    # One block per scenario, so one tee session per scenario holds all its tasks.
    blocks = [t.scenario for t in tasks]
    assert [b for i, b in enumerate(blocks) if i == 0 or blocks[i - 1] != b] == sorted(
        set(blocks), key=blocks.index
    )


def test_a_larger_count_keeps_every_task_and_its_parameters() -> None:
    small = {(t.scenario, t.index): t.params for t in scenarios.plan(7, 6)}
    large = {(t.scenario, t.index): t.params for t in scenarios.plan(7, 9)}
    assert small.items() <= large.items()


def test_repeats_differ_by_their_parameters() -> None:
    for scenario in scenarios.SCENARIOS:
        params = [t.params for t in scenarios.plan(7, 6) if t.scenario == scenario]
        assert len({tuple(sorted(p.items())) for p in params}) > 1, scenario
    causes = Counter(t.params["cause"] for t in scenarios.plan(7, 6) if t.scenario == "crash")
    assert causes == dict.fromkeys(scenarios.CRASH_CAUSES, 2)


def test_every_workload_is_named_after_its_deployment() -> None:
    for task in scenarios.plan(7, 6):
        objects = scenarios.manifests(task, IMAGE)
        assert scenarios.check_names(objects) == []
        assert objects[0]["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
        assert objects[0]["metadata"]["labels"][scenarios.LABEL] == "workload"


def test_check_names_catches_a_container_named_otherwise() -> None:
    task = scenarios.plan(7, 6)[0]
    objects = scenarios.manifests(task, IMAGE)
    objects[0]["spec"]["template"]["spec"]["containers"][0]["name"] = "app"
    assert scenarios.check_names(objects) == [f"deployment {task.name}: containers ['app']"]


def test_the_scenario_faults() -> None:
    tasks = {t.name: t for t in scenarios.plan(7, 6)}
    oom = scenarios.manifests(tasks["oom-0"], IMAGE)[0]["spec"]["template"]["spec"]["containers"][0]
    assert oom["resources"]["limits"]["memory"] == f"{tasks['oom-0'].params['memory_limit_mi']}Mi"
    assert scenarios.TRIGGER in oom["command"][2]
    latency = scenarios.manifests(tasks["latency-0"], IMAGE)
    assert latency[1]["metadata"]["labels"]["traceblame.io/probe"] == "http"
    pending = {
        t.params["cause"]: scenarios.manifests(t, IMAGE)[0]
        for t in tasks.values()
        if t.scenario == "pending"
    }
    assert pending["node_selector"]["spec"]["template"]["spec"]["nodeSelector"]
    assert (
        pending["unsatisfiable_cpu"]["spec"]["template"]["spec"]["containers"][0]["resources"][
            "requests"
        ]["cpu"]
        == "64"
    )
    claim = pending["missing_pvc"]["spec"]["template"]["spec"]["volumes"][1][
        "persistentVolumeClaim"
    ]
    assert claim["claimName"].endswith("-data")


def test_the_api_error_task_is_one_more_crash_task_outside_the_plan() -> None:
    tasks = scenarios.plan(7, 6)
    extra = scenarios.api_error_task(7, 6)
    assert extra == scenarios.api_error_task(7, 6)
    assert extra.scenario == "crash"
    assert extra.approval is None
    assert extra.order == len(tasks)
    assert extra.name not in {t.name for t in tasks}
    # The same parameters a larger plan would give that task.
    assert extra.params == next(
        t.params for t in scenarios.plan(7, 7) if (t.scenario, t.index) == ("crash", 6)
    )
    # The triage call fails, so the app declines: no runbook.
    assert extra.expected_runbook == "none"
    assert not scenarios.check_names(scenarios.manifests(extra, IMAGE))
