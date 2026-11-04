"""Every alert links its runbook, and the runbook exists (CLAUDE.md rule 6)."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RULE_FILES = sorted((ROOT / "deploy" / "alerts").glob("*.rules.yml"))
BLOB = "https://github.com/arec1b0/traceblame/blob/main/docs/runbooks/"

pytestmark = pytest.mark.contract


def _alerts() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for path in RULE_FILES:
        doc = cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))
        for group in doc["groups"]:
            out.extend((f"{path.name}:{r['alert']}", r) for r in group["rules"] if "alert" in r)
    return out


def test_there_are_alert_rules() -> None:
    assert RULE_FILES
    assert {a for _, a in [(n, r["alert"]) for n, r in _alerts()]} >= {
        "TraceSummaryReconciliationMiss",
        "AssemblerWatermarkLagHigh",
    }


@pytest.mark.parametrize(("name", "rule"), _alerts(), ids=lambda v: v if isinstance(v, str) else "")
def test_an_alert_links_an_existing_runbook_named_after_it(name: str, rule: dict[str, Any]) -> None:
    url = rule["annotations"]["runbook_url"]
    assert url == f"{BLOB}{rule['alert']}.md", name
    assert (ROOT / "docs" / "runbooks" / f"{rule['alert']}.md").is_file(), name


@pytest.mark.parametrize(("name", "rule"), _alerts(), ids=lambda v: v if isinstance(v, str) else "")
def test_an_alert_has_a_severity_and_a_wait(name: str, rule: dict[str, Any]) -> None:
    assert rule["labels"]["severity"] in {"page", "ticket"}, name
    assert rule["for"], name
