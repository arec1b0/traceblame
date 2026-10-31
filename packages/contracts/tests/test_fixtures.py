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
