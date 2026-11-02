"""The runbook-agent tasks of a capture (#13): real faults in the sandbox, seeded.

Four scenarios, each a fault that a pinned upstream alert rule detects (rules.py):

  oom        a container past its memory limit        OOMKilled                  oom-restart
  latency    an HTTP service slow under load          HighRequestLatency         high-load-scale-up
  crash      a container that keeps exiting           KubernetesPodCrashLooping  investigate-unknown
  pending    a pod that cannot start                  KubernetesPodNotHealthy    investigate-unknown

The seed fixes every parameter, the order and the approval decisions; plan() with the same seed
and a larger count gives a superset. Every workload's container is named after its deployment,
which is what the rules' `service` label reads (check_names).
"""

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

NAMESPACE = "runbook-sandbox"
SCENARIOS = ("oom", "latency", "crash", "pending")
ALERT = {
    "oom": "OOMKilled",
    "latency": "HighRequestLatency",
    "crash": "KubernetesPodCrashLooping",
    "pending": "KubernetesPodNotHealthy",
}
EXPECTED_RUNBOOK = {
    "oom": "oom-restart",
    "latency": "high-load-scale-up",
    "crash": "investigate-unknown",
    "pending": "investigate-unknown",
}
# Scenarios whose runbook has a step the approval gate holds (restart, scale).
APPROVAL_SCENARIOS = ("oom", "latency")
CRASH_CAUSES = ("exit_code", "missing_config", "failing_liveness")
PENDING_CAUSES = ("unsatisfiable_cpu", "missing_pvc", "node_selector")
# The OOM rule compares restarts with 10 minutes ago, so it sees only pods at least that old:
# oom workloads start healthy at the beginning of a run and are triggered later.
OOM_MIN_AGE_SECONDS = 11 * 60
LABEL = "traceblame.io/role"
PROBE = "traceblame.io/probe"
TRIGGER = "/tmp/trigger"  # noqa: S108 - inside the workload's own emptyDir


@dataclass(frozen=True)
class Task:
    scenario: str
    index: int
    params: dict[str, Any]
    approval: str | None  # "approve", "reject", "timeout"; None when no step needs approval
    order: int = field(default=0, compare=False)
    runbook: str | None = None  # the expected runbook, when not the scenario's

    @property
    def name(self) -> str:
        return f"{self.scenario}-{self.index}"

    @property
    def alert(self) -> str:
        return ALERT[self.scenario]

    @property
    def expected_runbook(self) -> str:
        return self.runbook or EXPECTED_RUNBOOK[self.scenario]


def _rank(seed: int, key: str) -> str:
    return hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()


def _params(seed: int, scenario: str, index: int) -> dict[str, Any]:
    rng = random.Random(_rank(seed, f"{scenario}:{index}"))  # noqa: S311 - test parameters
    if scenario == "oom":
        return {
            "memory_limit_mi": rng.choice([48, 64, 80, 96]),
            "alloc_mib_per_s": rng.randint(8, 32),
        }
    if scenario == "latency":
        return {"delay_s": round(rng.uniform(1.2, 3.0), 1), "load_rps": rng.choice([1, 2, 4])}
    if scenario == "crash":
        cause = CRASH_CAUSES[index % len(CRASH_CAUSES)]
        return (
            {"cause": cause, "exit_code": rng.randint(1, 3)}
            if cause == "exit_code"
            else {"cause": cause}
        )
    return {"cause": PENDING_CAUSES[index % len(PENDING_CAUSES)]}


def plan(seed: int, per_scenario: int, *, timeouts: int = 2, rejections: int = 2) -> list[Task]:
    """per_scenario tasks of each scenario, ordered by scenario block (the seed orders the blocks
    and the tasks within them). Of the tasks whose runbook needs approval, `timeouts` go
    unanswered and `rejections` are rejected, chosen by the seed; the rest are approved."""
    approval_tasks = sorted(
        ((s, i) for s in APPROVAL_SCENARIOS for i in range(per_scenario)),
        key=lambda t: _rank(seed, f"approval:{t[0]}:{t[1]}"),
    )
    if timeouts + rejections > len(approval_tasks):
        raise ValueError("more timeouts and rejections than approval tasks")
    decision = dict.fromkeys(approval_tasks[:timeouts], "timeout")
    decision |= dict.fromkeys(approval_tasks[timeouts : timeouts + rejections], "reject")
    blocks = sorted(SCENARIOS, key=lambda s: _rank(seed, f"block:{s}"))
    tasks: list[Task] = []
    for scenario in blocks:
        indices = sorted(range(per_scenario), key=lambda i: _rank(seed, f"task:{scenario}:{i}"))
        for i in indices:
            approval = (
                decision.get((scenario, i), "approve") if scenario in APPROVAL_SCENARIOS else None
            )
            tasks.append(Task(scenario, i, _params(seed, scenario, i), approval, order=len(tasks)))
    return tasks


def api_error_task(seed: int, per_scenario: int) -> Task:
    """One crash task after the plan, for a run with a model the server does not have: the
    triage call fails with a real HTTP 404, so the app declines to remediate (no runbook)."""
    return Task(
        "crash",
        per_scenario,
        _params(seed, "crash", per_scenario),
        None,
        order=len(SCENARIOS) * per_scenario,
        runbook="none",
    )


# Workloads ----------------------------------------------------------------------------------

_OOM = """\
import os, time
while not os.path.exists({trigger!r}):
    time.sleep(1)
held = []
while True:
    held.append(bytearray({chunk} * 1024 * 1024))
    time.sleep(0.25)
"""

_SLOW = """\
import threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Slow(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep({delay})
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

def load():
    while True:
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/", timeout=10).read()
        except OSError:
            pass
        time.sleep(1 / {rps})

for _ in range({rps}):
    threading.Thread(target=load, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", 8080), Slow).serve_forever()
"""


def _container(task: Task, image: str) -> dict[str, Any]:
    p = task.params
    container: dict[str, Any] = {
        "name": task.name,
        "image": image,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65534,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],  # noqa: S108
    }
    if task.scenario == "oom":
        script = _OOM.format(trigger=TRIGGER, chunk=max(1, p["alloc_mib_per_s"] // 4))
        container["command"] = ["python", "-c", script]
        container["resources"]["limits"]["memory"] = f"{p['memory_limit_mi']}Mi"
    elif task.scenario == "latency":
        container["command"] = ["python", "-c", _SLOW.format(delay=p["delay_s"], rps=p["load_rps"])]
        container["ports"] = [{"name": "http", "containerPort": 8080}]
    elif task.scenario == "crash":
        if p["cause"] == "exit_code":
            container["command"] = [
                "python",
                "-c",
                f"import sys, time; time.sleep(2); sys.exit({p['exit_code']})",
            ]
        elif p["cause"] == "missing_config":
            container["command"] = ["python", "-c", "open('/etc/app/config.yaml').read()"]
        else:
            container["command"] = ["python", "-c", "import time; time.sleep(3600)"]
            container["livenessProbe"] = {
                "httpGet": {"path": "/healthz", "port": 9999},
                "initialDelaySeconds": 1,
                "periodSeconds": 2,
                "failureThreshold": 1,
            }
    else:
        container["command"] = ["python", "-c", "import time; time.sleep(3600)"]
        if p["cause"] == "unsatisfiable_cpu":
            container["resources"]["requests"]["cpu"] = "64"
        elif p["cause"] == "missing_pvc":
            container["volumeMounts"].append({"name": "data", "mountPath": "/data"})
    return container


def manifests(task: Task, image: str) -> list[dict[str, Any]]:
    """The task's Deployment (and Service for the latency scenario)."""
    labels = {"app": task.name, LABEL: "workload", "traceblame.io/scenario": task.scenario}
    volumes: list[dict[str, Any]] = [{"name": "tmp", "emptyDir": {}}]
    pod: dict[str, Any] = {
        "serviceAccountName": "workload",
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 1,
        "containers": [_container(task, image)],
        "volumes": volumes,
    }
    if task.scenario == "pending" and task.params["cause"] == "missing_pvc":
        volumes.append(
            {"name": "data", "persistentVolumeClaim": {"claimName": f"{task.name}-data"}}
        )
    if task.scenario == "pending" and task.params["cause"] == "node_selector":
        pod["nodeSelector"] = {"traceblame.io/no-such-node": "true"}
    out: list[dict[str, Any]] = [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": task.name, "namespace": NAMESPACE, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": task.name}},
                "template": {"metadata": {"labels": labels}, "spec": pod},
            },
        }
    ]
    if task.scenario == "latency":
        out.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": task.name,
                    "namespace": NAMESPACE,
                    "labels": {PROBE: "http", LABEL: "workload"},
                },
                "spec": {
                    "selector": {"app": task.name},
                    "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
                },
            }
        )
    return out


def check_names(objects: list[dict[str, Any]]) -> list[str]:
    """Problems with the label sources of the rules' `service`: every Deployment's containers
    are named after it, and every probed Service is too (blackbox `instance`)."""
    problems: list[str] = []
    for obj in objects:
        name = obj["metadata"]["name"]
        if obj["kind"] == "Deployment":
            containers = obj["spec"]["template"]["spec"]["containers"]
            if [c["name"] for c in containers] != [name]:
                problems.append(f"deployment {name}: containers {[c['name'] for c in containers]}")
        elif obj["kind"] == "Service" and obj["metadata"].get("labels", {}).get(
            "traceblame.io/probe"
        ):
            if obj["spec"]["selector"].get("app") != name:
                problems.append(f"service {name}: selects {obj['spec']['selector']}")
    return problems
