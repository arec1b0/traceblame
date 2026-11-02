"""The runbook sandbox's alert rules: upstream expressions verbatim, every override listed (#13)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

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


rules = _load("rules")

# The overrides approved in #13 (expression and annotations never change).
EXPECTED = [
    ("OOMKilled", "alert", "KubernetesContainerOomKiller", "OOMKilled"),
    ("OOMKilled", "labels.service", None, "{{ $labels.container }}"),
    ("HighRequestLatency", "alert", "BlackboxProbeSlowHttp", "HighRequestLatency"),
    ("HighRequestLatency", "for", "1m", "30s"),
    ("HighRequestLatency", "labels.namespace", None, "runbook-sandbox"),
    ("HighRequestLatency", "labels.service", None, "{{ $labels.instance }}"),
    ("HighRequestLatency", "labels.severity", "warning", "critical"),
    ("KubernetesPodCrashLooping", "for", "2m", "30s"),
    ("KubernetesPodCrashLooping", "labels.service", None, "{{ $labels.container }}"),
    ("KubernetesPodNotHealthy", "for", "15m", "1m"),
    (
        "KubernetesPodNotHealthy",
        "labels.service",
        None,
        '{{ reReplaceAll "-[a-z0-9]+-[a-z0-9]+$" "" $labels.pod }}',
    ),
]


def test_overrides_are_exactly_the_approved_ones() -> None:
    got = [(o["rule"], o["field"], o["original"], o["used"]) for o in rules.overrides()]
    assert got == EXPECTED


def test_every_rule_has_its_upstream_expression_and_annotations() -> None:
    upstream = rules.upstream_rules()
    for name, rule in rules.local_rules().items():
        source = upstream[rules.SOURCES[name]]
        assert rule["expr"] == source["expr"]
        assert rule["annotations"] == source["annotations"]


def test_a_changed_expression_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = yaml.safe_load(rules.RULES.read_text(encoding="utf-8"))
    data["groups"][0]["rules"][0]["expr"] += " > 0"
    changed = tmp_path / "rules.yml"
    changed.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setattr(rules, "RULES", changed)
    with pytest.raises(ValueError, match="expr differs"):
        rules.overrides()


def test_vendored_files_must_match_their_checksum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in rules.UPSTREAM_FILES:
        (tmp_path / "upstream").mkdir(exist_ok=True)
        (tmp_path / "upstream" / name).write_bytes((rules.HERE / "upstream" / name).read_bytes())
    (tmp_path / "upstream" / "blackbox-exporter.yml").write_text("groups: []\n", encoding="utf-8")
    monkeypatch.setattr(rules, "HERE", tmp_path)
    with pytest.raises(ValueError, match="pinned sha256"):
        rules.upstream_rules()


def test_provenance_names_the_source() -> None:
    p = rules.provenance()
    assert p["commit"] == rules.UPSTREAM_COMMIT
    assert {f["path"] for f in p["files"]} == set(rules.UPSTREAM_FILES.values())
    assert len(p["overrides"]) == len(EXPECTED)
