import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceblame_contracts.fixtures import FixtureError, discover_fixtures, sha256_file

_OTLP: dict[str, Any] = {"resourceSpans": []}


def _provenance(fixture_sha256: str) -> dict[str, Any]:
    return {
        "sdk": "openinference",
        "scenario": "chat_basic",
        "captured_at": "2026-10-30T18:00:00Z",
        "capture_script": "tools/fixtures/capture_openinference.py",
        "capture_script_sha256": "0" * 64,
        "python": "3.12.15",
        "uv": "0.13.6",
        "packages": {"openinference-instrumentation-openai": "0.1.68"},
        "env": {"OPENINFERENCE_HIDE_INPUTS": "true"},
        "model": {
            "name": "qwen2.5:7b",
            "digest": "845dbda0ea48",
            "server": "ollama",
            "server_version": "0.40.5",
        },
        "endpoint": "http://172.22.96.1:11434/v1",
        "collector": "otelcol-contrib 0.164.0",
        "fixture_sha256": fixture_sha256,
    }


def _write_fixture(directory: Path, name: str = "chat_basic") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fixture = directory / f"{name}.otlp.json"
    fixture.write_text(json.dumps(_OTLP) + "\n", encoding="utf-8")
    provenance = _provenance(sha256_file(fixture))
    (directory / f"{name}.provenance.yaml").write_text(yaml.safe_dump(provenance), "utf-8")
    return fixture


def test_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    assert sha256_file(path) == hashlib.sha256(b"abc").hexdigest()


def test_discover_pairs_fixtures_with_provenance(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "openinference" / "openinference-instrumentation-openai-0.1.68")
    (fixture,) = discover_fixtures(tmp_path)
    assert fixture.id == "openinference/openinference-instrumentation-openai-0.1.68/chat_basic"
    assert fixture.provenance().sdk == "openinference"
    assert fixture.load() == _OTLP
    assert fixture.hash_matches()


def test_discover_is_sorted(tmp_path: Path) -> None:
    _write_fixture(tmp_path / "b", "x")
    _write_fixture(tmp_path / "a", "y")
    assert [f.id for f in discover_fixtures(tmp_path)] == ["a/y", "b/x"]


def test_missing_provenance_is_an_error(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path)
    fixture.with_name("chat_basic.provenance.yaml").unlink()
    with pytest.raises(FixtureError, match="provenance"):
        discover_fixtures(tmp_path)


def test_orphan_provenance_is_an_error(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path)
    fixture.unlink()
    with pytest.raises(FixtureError, match=r"chat_basic\.otlp\.json"):
        discover_fixtures(tmp_path)


def test_edited_fixture_no_longer_matches_its_hash(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path)
    fixture.write_text(json.dumps({"resourceSpans": [{}]}) + "\n", encoding="utf-8")
    (found,) = discover_fixtures(tmp_path)
    assert not found.hash_matches()


def test_provenance_rejects_unknown_fields(tmp_path: Path) -> None:
    fixture = _write_fixture(tmp_path)
    path = fixture.with_name("chat_basic.provenance.yaml")
    data = yaml.safe_load(path.read_text("utf-8"))
    data["edited_by_hand"] = True
    path.write_text(yaml.safe_dump(data), "utf-8")
    (found,) = discover_fixtures(tmp_path)
    with pytest.raises(FixtureError, match="provenance"):
        found.provenance()
