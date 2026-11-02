import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceblame_contracts.fixtures import FixtureError, discover_fixtures, sha256_file

WriteFixture = Callable[..., Path]


def test_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    assert sha256_file(path) == hashlib.sha256(b"abc").hexdigest()


def test_discover_pairs_fixtures_with_provenance(
    tmp_path: Path, write_fixture: WriteFixture
) -> None:
    write_fixture(tmp_path / "openinference" / "openinference-instrumentation-openai-0.1.68")
    (fixture,) = discover_fixtures(tmp_path)
    assert fixture.id == "openinference/openinference-instrumentation-openai-0.1.68/chat_basic"
    assert fixture.provenance().sdk == "openinference"
    assert fixture.load() == {"resourceSpans": []}
    assert fixture.hash_matches()


def test_discover_is_sorted(tmp_path: Path, write_fixture: WriteFixture) -> None:
    write_fixture(tmp_path / "b", "x")
    write_fixture(tmp_path / "a", "y")
    assert [f.id for f in discover_fixtures(tmp_path)] == ["a/y", "b/x"]


def test_missing_provenance_is_an_error(tmp_path: Path, write_fixture: WriteFixture) -> None:
    fixture = write_fixture(tmp_path)
    fixture.with_name("chat_basic.provenance.yaml").unlink()
    with pytest.raises(FixtureError, match="provenance"):
        discover_fixtures(tmp_path)


def test_orphan_provenance_is_an_error(tmp_path: Path, write_fixture: WriteFixture) -> None:
    fixture = write_fixture(tmp_path)
    fixture.unlink()
    with pytest.raises(FixtureError, match=r"chat_basic\.otlp\.json"):
        discover_fixtures(tmp_path)


def test_edited_fixture_no_longer_matches_its_hash(
    tmp_path: Path, write_fixture: WriteFixture
) -> None:
    fixture = write_fixture(tmp_path)
    fixture.write_text(json.dumps({"resourceSpans": [{}]}) + "\n", encoding="utf-8")
    (found,) = discover_fixtures(tmp_path)
    assert not found.hash_matches()


def test_provenance_rejects_unknown_fields(tmp_path: Path, write_fixture: WriteFixture) -> None:
    fixture = write_fixture(tmp_path)
    path = fixture.with_name("chat_basic.provenance.yaml")
    data = yaml.safe_load(path.read_text("utf-8"))
    data["edited_by_hand"] = True
    path.write_text(yaml.safe_dump(data), "utf-8")
    (found,) = discover_fixtures(tmp_path)
    with pytest.raises(FixtureError, match="provenance"):
        found.provenance()


_APP_CAPTURE: dict[str, Any] = {
    "capture_script": "tools/fixtures/capture_agentic_rag.py",
    "content_check": "passed",
    "app": {"repository": "https://github.com/arec1b0/agentic-rag", "commit": "a" * 40},
    "shim": {"package": "traceblame-py", "version": "0.1.0", "commit": "b" * 40},
    "dataset": {
        "name": "CUAD v1, official test split",
        "source": "https://example.invalid/data.zip",
        "archive_sha256": "c" * 64,
        "license": "CC BY 4.0",
        "seed": 7,
        "n": 24,
        "ratio": 0.5,
        "stratum": "answerable",
        "qids": ["T__Document Name"],
    },
    "embedding_model": {"name": "sentence-transformers/all-MiniLM-L6-v2", "revision": "d" * 40},
    "run": {
        "script": "tools/fixtures/agentic_rag/run.py",
        "script_sha256": "e" * 64,
        "outcomes": {"answered": 12},
        "report": "evals/reports/sdk-py-kind-2026-10-31.md",
    },
}


def _rewrite_provenance(
    fixture: Path, changes: dict[str, object], drop: tuple[str, ...] = ()
) -> None:
    path = fixture.with_name(fixture.name.removesuffix(".otlp.json") + ".provenance.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) | changes
    for key in drop:
        data.pop(key)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_app_capture_provenance(tmp_path: Path, write_fixture: WriteFixture) -> None:
    fixture = write_fixture(tmp_path)
    _rewrite_provenance(fixture, _APP_CAPTURE, drop=("capture_common_sha256",))
    (found,) = discover_fixtures(tmp_path)
    provenance = found.provenance()
    assert provenance.capture_common_sha256 is None
    assert provenance.content_check == "passed"
    assert provenance.dataset is not None
    assert provenance.dataset.qids == ("T__Document Name",)
    assert provenance.app is not None
    assert provenance.app.commit == "a" * 40


@pytest.mark.parametrize(
    "changes",
    [
        {"content_check": "skipped"},
        {"app": {"repository": "r", "commit": "short"}},
        {"dataset": {**_APP_CAPTURE["dataset"], "stratum": "some"}},
        {"run": {**_APP_CAPTURE["run"], "extra": 1}},
    ],
)
def test_app_capture_provenance_is_strict(
    tmp_path: Path, write_fixture: WriteFixture, changes: dict[str, Any]
) -> None:
    fixture = write_fixture(tmp_path)
    _rewrite_provenance(fixture, _APP_CAPTURE | changes)
    (found,) = discover_fixtures(tmp_path)
    with pytest.raises(FixtureError, match="invalid provenance"):
        found.provenance()


# A TypeScript application capture (#13): node and pnpm instead of python and uv, a run from
# a built image, and the alert scenario of the run.
_TS_CAPTURE: dict[str, Any] = {
    "sdk": "openllmetry",
    "node": "v22.23.4",
    "pnpm": "10.34.9",
    "content_check": "passed",
    "app": {"repository": "https://github.com/arec1b0/runbook-agent", "commit": "c" * 40},
    "shim": {"package": "traceblame-ts", "version": "0.1.0", "commit": "d" * 40},
    "run": {
        "report": "evals/reports/sdk-ts-kind-2026-11-01.md",
        "eval_run_id": "rb-20261101-s7-abcdef1",
        "tenant": "tenant-eval",
        "image": "runbook-agent:c-d",
        "image_id": "sha256:" + "e" * 64,
    },
    "scenario_run": {
        "seed": 7,
        "per_scenario": 6,
        "alert": "OOMKilled",
        "expected_runbook": "oom-restart",
        "rules": {
            "repository": "https://github.com/samber/awesome-prometheus-alerts",
            "commit": "f" * 40,
            "files": [{"path": "dist/rules/kubernetes/kubestate-exporter.yml", "sha256": "0" * 64}],
            "license": "CC BY 4.0",
            "verbatim": ["expr", "annotations"],
            "overrides": [
                {
                    "rule": "OOMKilled",
                    "upstream": "KubernetesContainerOomKiller",
                    "field": "alert",
                    "original": "KubernetesContainerOomKiller",
                    "used": "OOMKilled",
                }
            ],
        },
        "approver": "scripted:7",
        "approval_timeout_ms": 30000,
        "tasks": [
            {
                "task": "oom-5",
                "scenario": "oom",
                "order": 6,
                "params": {"memory_limit_mi": 64, "alloc_mib_per_s": 10},
                "alert": "OOMKilled",
                "expected_runbook": "oom-restart",
                "runbook": "oom-restart",
                "status": "remediated",
                "steps": [{"action": "collect-diagnostics", "status": "executed"}],
                "approval_policy": "approve",
                "approvals": [{"action": "restart-service", "decision": "approve"}],
                "alerts_received": 1,
                "webhooks": 1,
                "webhooks_failed": 0,
                "started_at": "2026-11-01T10:00:00Z",
                "session": "oom",
            }
        ],
    },
    "monitoring": {"PROMETHEUS_IMAGE": "prom/prometheus:v3.15.1@sha256:" + "1" * 64},
}


def test_typescript_app_capture_provenance(tmp_path: Path, write_fixture: WriteFixture) -> None:
    fixture = write_fixture(tmp_path)
    _rewrite_provenance(fixture, _TS_CAPTURE, drop=("capture_common_sha256", "python", "uv"))
    (found,) = discover_fixtures(tmp_path)
    provenance = found.provenance()
    assert provenance.python is None
    assert provenance.node == "v22.23.4"
    assert provenance.scenario_run is not None
    assert provenance.scenario_run.tasks[0].approvals[0].decision == "approve"
    assert provenance.scenario_run.rules.overrides[0].used == "OOMKilled"
    assert provenance.scenario_run.tasks[0].session == "oom"


@pytest.mark.parametrize(
    ("changes", "drop"),
    [
        ({}, ("capture_common_sha256", "python", "uv", "node")),  # a runtime half missing
        ({}, ("capture_common_sha256",)),  # both runtimes at once
        ({"scenario_run": {**_TS_CAPTURE["scenario_run"], "approver": "alice"}}, ("python", "uv")),
        (
            {
                "scenario_run": {
                    **_TS_CAPTURE["scenario_run"],
                    "tasks": [
                        {**_TS_CAPTURE["scenario_run"]["tasks"][0], "approval_policy": "maybe"}
                    ],
                }
            },
            ("python", "uv"),
        ),
        ({"run": {**_TS_CAPTURE["run"], "image_id": "latest"}}, ("python", "uv")),
        (
            {
                "scenario_run": {
                    **_TS_CAPTURE["scenario_run"],
                    "tasks": [{**_TS_CAPTURE["scenario_run"]["tasks"][0], "webhooks_failed": -1}],
                }
            },
            ("python", "uv"),
        ),
    ],
)
def test_typescript_app_capture_provenance_is_strict(
    tmp_path: Path, write_fixture: WriteFixture, changes: dict[str, Any], drop: tuple[str, ...]
) -> None:
    fixture = write_fixture(tmp_path)
    _rewrite_provenance(fixture, _TS_CAPTURE | changes, drop=drop)
    (found,) = discover_fixtures(tmp_path)
    with pytest.raises(FixtureError, match="invalid provenance"):
        found.provenance()
