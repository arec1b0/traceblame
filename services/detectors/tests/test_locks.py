"""Version locks and golden vectors: framework or plugin changes need a new version."""

import json
import shutil
from pathlib import Path
from typing import Any, cast

from traceblame_detectors.config import detector_version
from traceblame_detectors.engine import FRAMEWORK_VERSION, Finding, finding_id
from traceblame_detectors.locks import (
    FRAMEWORK_LOCK,
    PLUGINS_LOCK,
    append_only_problems,
    framework_problems,
    framework_sources,
    load_lock,
    plugin_problems,
    plugin_sha,
)
from traceblame_detectors.plugins import REGISTRY
from traceblame_detectors.testing import load_detector

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "v1.json"


def test_the_framework_sources_match_the_lock_of_this_version() -> None:
    sources = framework_sources()
    assert len(sources) == 5
    assert framework_problems(load_lock(FRAMEWORK_LOCK), FRAMEWORK_VERSION, sources) == []


def test_a_changed_framework_module_without_a_bump_fails() -> None:
    sources = {**framework_sources(), "engine.py": "0" * 64}
    problems = framework_problems(load_lock(FRAMEWORK_LOCK), FRAMEWORK_VERSION, sources)
    assert problems == [
        f"engine.py changed under FRAMEWORK_VERSION {FRAMEWORK_VERSION}: "
        "bump it and add a lock entry"
    ]
    assert framework_problems(load_lock(FRAMEWORK_LOCK), "9.9.9", sources)


def test_shipped_plugins_match_their_lock() -> None:
    checked, problems = plugin_problems(REGISTRY, load_lock(PLUGINS_LOCK))
    assert problems == []
    assert checked == len(load_lock(PLUGINS_LOCK)) == len(REGISTRY)  # 0 until #16


def test_the_plugin_lock_catches_a_change_without_a_bump(tmp_path: Path) -> None:
    src = Path(__file__).parent / "plugins" / "reference_root"
    copy = tmp_path / "reference_root"
    shutil.copytree(src, copy)
    cls = load_detector(copy / "detector.py")
    lock = {"reference_root": {"0.1.0": plugin_sha(cls)}}
    assert plugin_problems({"reference_root": cls}, lock) == (1, [])
    (copy / "detector.py").write_text((copy / "detector.py").read_text() + "\n# edited\n")
    edited = load_detector(copy / "detector.py")
    checked, problems = plugin_problems({"reference_root": edited}, lock)
    assert checked == 1
    assert problems == [
        "reference_root changed under code_version 0.1.0: bump it and add a lock entry"
    ]
    assert plugin_problems({"reference_root": edited}, {})[1] == [
        f"reference_root 0.1.0: no entry in {PLUGINS_LOCK}"
    ]
    assert plugin_problems({}, lock)[1] == ["reference_root: locked but not registered"]


def test_locks_are_append_only() -> None:
    base: dict[str, Any] = {"versions": {"1.0.0": {"engine.py": "a"}}}
    assert (
        append_only_problems(base, {"versions": {"1.0.0": {"engine.py": "a"}, "1.1.0": {}}}) == []
    )
    assert append_only_problems(base, {"versions": {"1.0.0": {"engine.py": "b"}}}) == [
        "/versions/1.0.0/engine.py changed"
    ]
    assert append_only_problems(base, {"versions": {}}) == ["/versions/1.0.0 was removed"]


def _vectors() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert doc["framework_version"] == FRAMEWORK_VERSION
    return doc


def test_version_strings_match_the_vectors() -> None:
    entries = _vectors()["versions"]
    assert entries
    for v in entries:
        assert detector_version(v["code_version"], v["params_json"]) == v["detector_version"]


def test_finding_ids_match_the_vectors() -> None:
    entries = _vectors()["finding_ids"]
    assert entries
    for v in entries:
        args = (
            v["trace_id"],
            v["detector"],
            v["detector_version"],
            v["span_id"],
            v["revision_ms"],
            v["input_digest"],
        )
        assert finding_id(*args) == v["finding_id"]


def test_canonical_finding_bytes_match_the_vectors() -> None:
    entries = _vectors()["findings"]
    assert entries
    for v in entries:
        f = v["finding"]
        pairs = cast("list[tuple[str, Any]]", f.pop("evidence"))
        evidence = tuple(
            (k, tuple(cast("list[Any]", x)) if isinstance(x, list) else x) for k, x in pairs
        )
        finding = Finding(**f, evidence=evidence)
        assert finding.canonical_bytes().decode() == v["canonical"]
