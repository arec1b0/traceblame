"""bench/gateway-scaling/run.sh touches the collectors only when it gets past its checks.

A run that preflight rejects, for example because another bench is running, must leave the
collectors' args as they are: restoring them would strip the running bench's overlays and roll
the collectors in the middle of its measurement. A run that changed the args restores them on
any exit. A fake kubectl records every call; nothing reaches a cluster.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "bench" / "gateway-scaling" / "run.sh"

FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >>"${FAKE_LOG}"
args=" $* "
case "${args}" in
  *" get pod -l clickhouse.altinity.com/chi=traceblame "*) echo chi-traceblame-demo-0-0-0 ;;
  *" get secret "*) printf 'dXNlcg==' ;;  # base64 "user"
  *" exec chi-traceblame-demo-0-0-0 "*)
    [[ "${FAKE_CH_FAIL:-0}" == 1 ]] && exit 1
    echo "0001 spans (expand, ec39fa3ba092, 2026-11-05T13:23:49Z)" ;;
  *" exec postgres-0 "*)
    echo "0001 assembler_partitions (expand, 8ae1a3710e21, 2026-11-04T15:31:58Z)" ;;
  *" get pods -A --no-headers "*) cat "${FAKE_PODS}" ;;
  *" get jobs -A "*) ;;
  *" rollout status "*) [[ "${FAKE_ROLLOUT_FAIL:-0}" == 1 ]] && exit 1 ;;
esac
exit 0
"""

IDLE_PODS = (
    "observability otel-agent-a 1/1 Running 0 1h\nobservability otel-gateway-a 1/1 Running 0 1h\n"
)
# The marker of a bench that is running: its load generator pod.
RUNNING_BENCH = IDLE_PODS + "bench loadgen-0-abcde 1/1 Running 0 30s\n"

PLAIN_AGENT = '["--config=/etc/otelcol/config/agent.yaml"]'
PLAIN_GATEWAY = '["--config=/etc/otelcol/config/gateway.yaml"]'


def run(tmp_path: Path, pods: str, **env: str) -> tuple[int, list[str], str]:
    fake = tmp_path / "kubectl"
    fake.write_text(FAKE_KUBECTL, encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "pods.txt").write_text(pods, encoding="utf-8")
    log = tmp_path / "kubectl.log"
    log.touch()
    proc = subprocess.run(  # noqa: S603 - the repository's own script with a fake kubectl
        ["bash", str(RUN_SH)],  # noqa: S607
        cwd=ROOT,
        env={
            **os.environ,
            "KUBECTL": str(fake),
            "FAKE_LOG": str(log),
            "FAKE_PODS": str(tmp_path / "pods.txt"),
            "MAX_HOST_LOAD": "100000",
            # No other session's real capture may decide this test.
            "BUSY_PROCESSES": "^$a",
            "PROFILE": "low",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, log.read_text(encoding="utf-8").splitlines(), proc.stderr


def mutations(calls: list[str]) -> list[str]:
    return [
        c
        for c in calls
        if any(v in c.split() for v in ("patch", "rollout", "scale", "apply", "delete", "run"))
    ]


def test_a_run_rejected_by_preflight_leaves_the_collectors_alone(tmp_path: Path) -> None:
    rc, calls, err = run(tmp_path, RUNNING_BENCH, STAMPS="1")
    assert rc == 1
    assert "another bench or capture is running" in err
    assert any("get pods -A" in c for c in calls), "the preflight must have looked"
    assert mutations(calls) == []
    assert "restoring" not in err


def test_a_run_whose_schema_check_fails_leaves_the_collectors_alone(tmp_path: Path) -> None:
    rc, calls, err = run(tmp_path, IDLE_PODS, FAKE_CH_FAIL="1")
    assert rc == 1
    assert "schema_migrations" in err
    assert mutations(calls) == []


@pytest.mark.parametrize("stamps", ["0", "1"])
def test_a_run_that_changed_the_args_restores_them_when_it_fails(
    tmp_path: Path, stamps: str
) -> None:
    rc, calls, err = run(tmp_path, IDLE_PODS, STAMPS=stamps, FAKE_ROLLOUT_FAIL="1")
    assert rc != 0
    patches = [c for c in calls if " patch " in f" {c} "]
    # Two patches set the run's overlays, then two restore the plain args, after the failure.
    assert len(patches) == 4, calls
    failed_at = next(i for i, c in enumerate(calls) if "rollout status" in c)
    restores = [c for c in calls[failed_at:] if " patch " in f" {c} "]
    assert any("daemonset otel-agent" in c and PLAIN_AGENT in c for c in restores)
    assert any("deployment otel-gateway" in c and PLAIN_GATEWAY in c for c in restores)
    assert "restoring the agents and the gateway" in err
