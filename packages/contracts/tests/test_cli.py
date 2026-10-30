import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from traceblame_contracts.cli import main


@pytest.fixture
def profile_path(tmp_path: Path, minimal_profile: dict[str, Any]) -> Path:
    path = tmp_path / "aegis-telemetry.v1.yaml"
    path.write_text(yaml.safe_dump(minimal_profile, sort_keys=False), "utf-8")
    return path


def test_gen_schema_writes_next_to_the_profile(profile_path: Path) -> None:
    assert main(["gen-schema", "--profile", str(profile_path)]) == 0
    schema = profile_path.with_name("aegis-telemetry.v1.schema.json")
    assert json.loads(schema.read_text("utf-8"))["$id"] == "urn:aegis:schema:aegis-telemetry:1"
    assert b"\r\n" not in schema.read_bytes()


def test_gen_schema_check(profile_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gen-schema", "--profile", str(profile_path), "--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    main(["gen-schema", "--profile", str(profile_path)])
    assert main(["gen-schema", "--profile", str(profile_path), "--check"]) == 0


def test_validate_reports_violations(
    profile_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    otlp = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "scope": {"name": "s"},
                        "spans": [
                            {
                                "traceId": "4bf92f3577b34da6a3ce929d0e0e4736",
                                "spanId": "00f067aa0ba902b7",
                                "name": "x",
                                "kind": 1,
                                "startTimeUnixNano": "1",
                                "endTimeUnixNano": "2",
                            }
                        ],
                    }
                ]
            }
        ]
    }
    path = tmp_path / "t.otlp.json"
    path.write_text(json.dumps(otlp), "utf-8")
    assert main(["validate", str(path), "--profile", str(profile_path)]) == 1
    out = capsys.readouterr().out
    assert "00f067aa0ba902b7" in out
    assert "aegis.tenant.id" in out


def test_compat(
    profile_path: Path,
    tmp_path: Path,
    minimal_profile: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["compat", str(profile_path), str(profile_path)]) == 0
    minimal_profile["forbidden"].append({"name": "gen_ai.output.messages", "reason": "content"})
    newer = tmp_path / "newer.yaml"
    newer.write_text(yaml.safe_dump(minimal_profile, sort_keys=False), "utf-8")
    assert main(["compat", str(profile_path), str(newer)]) == 1
    out = capsys.readouterr().out
    assert "[major] forbidden_added" in out
    assert "required bump: major" in out
