"""The runbook driver's run phase, the parts that need no cluster (#70).

Found by the live smoke: a plan with latency tasks only made the driver apply an empty list of
oom workloads, which `kubectl apply` refuses. The same holds for a resume whose remaining
entries have no oom task."""

from typing import Any

import pytest
import run_runbook_agent
import scenarios


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(run_runbook_agent.rb, "apply", calls.append)
    monkeypatch.setattr(run_runbook_agent, "_python_image", lambda: "python:pinned")
    return calls


def _tasks(scenario: str) -> list[Any]:
    return [t for t in scenarios.plan(7, 3) if t.scenario == scenario]


def test_no_oom_task_applies_nothing(applied: list[list[dict[str, Any]]]) -> None:
    latency = _tasks("latency")
    assert len(latency) == 3
    assert run_runbook_agent.apply_oom_workloads(latency) == 0
    assert run_runbook_agent.apply_oom_workloads([]) == 0
    assert applied == [], "kubectl apply refuses an empty list"


def test_oom_tasks_are_applied_up_front_in_one_call(applied: list[list[dict[str, Any]]]) -> None:
    tasks = [*_tasks("latency"), *_tasks("oom")]
    assert run_runbook_agent.apply_oom_workloads(tasks) == 3
    (objects,) = applied
    names = {o["metadata"]["name"] for o in objects}
    assert names == {t.name for t in _tasks("oom")}
