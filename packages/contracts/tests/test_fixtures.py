import hashlib
import json
from collections.abc import Callable
from pathlib import Path

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
