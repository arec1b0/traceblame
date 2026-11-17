"""Plan files (#70): one shape for the committed plans and the #14 validation.yaml."""

from pathlib import Path
from typing import Any

import pytest

from traceblame_faults import kinds, planfile

D1: dict[str, Any] = {
    "seed": 19,
    "agentic_rag": {
        "n": 260,
        "ratio": 0.5,
        "queries": 110,
        "clean": 110,
        "counts": {"tool_failure": 75, "prompt_variant": 50},
    },
    "runbook_agent": {
        "scenarios": ["latency", "oom"],
        "per_scenario": 45,
        "distinct": True,
        "clean": 40,
        "counts": {"malformed_output": 25, "prompt_variant": 25},
    },
    "queries": "d1.queries.jsonl",
    "exclude": "validation.queries.jsonl",
    "split": {"seed": 19, "max_kind_deviation": 2},
}
VALIDATION: dict[str, Any] = {
    "seed": 7,
    "agentic_rag": {"n": 24, "ratio": 0.5, "queries": 4},
    "runbook_agent": {
        "scenarios": ["latency", "oom"],
        "per_scenario": 6,
        "clean": 3,
        "malformed_output": 3,
        "prompt_variant": 3,
    },
}


def test_a_plan_file_keeps_its_counts_and_takes_its_name_from_the_file(tmp_path: Path) -> None:
    import yaml

    path = tmp_path / "d1.yaml"
    path.write_text(yaml.safe_dump(D1), encoding="utf-8")
    cfg = planfile.load(path)
    assert (cfg["name"], cfg["seed"]) == ("d1", 19)
    assert planfile.composition(cfg) == {
        ("agentic-rag", "tool_failure"): 75,
        ("agentic-rag", "prompt_variant"): 50,
        ("agentic-rag", "clean"): 110,
        ("runbook-agent", "malformed_output"): 25,
        ("runbook-agent", "prompt_variant"): 25,
        ("runbook-agent", "clean"): 40,
    }
    assert cfg["runbook_agent"]["distinct"] is True
    assert len(planfile.sha256(path)) == 64


def test_the_validation_shape_becomes_every_kind_and_a_clean_run_per_query() -> None:
    cfg = planfile.normalize(VALIDATION, "validation")
    rag_kinds = sorted(k for k, v in kinds.KINDS.items() if "agentic-rag" in v.apps)
    assert cfg["agentic_rag"]["counts"] == dict.fromkeys(rag_kinds, 4)
    assert cfg["agentic_rag"]["clean"] == 4
    assert cfg["runbook_agent"]["counts"] == {"malformed_output": 3, "prompt_variant": 3}
    assert cfg["runbook_agent"]["distinct"] is False
    assert sum(planfile.composition(cfg).values()) == 4 * 7 + 9
    assert "split" not in cfg


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"seed": "7"}, "seed"),
        ({"agentic_rag": None}, "agentic_rag: missing"),
        ({"agentic_rag": D1["agentic_rag"] | {"counts": {"flood": 1}}}, "not a fault kind"),
        ({"runbook_agent": D1["runbook_agent"] | {"counts": {"tool_failure": 1}}}, "not a fault"),
        ({"agentic_rag": D1["agentic_rag"] | {"counts": {"tool_failure": -1}}}, "not a count"),
        ({"agentic_rag": D1["agentic_rag"] | {"clean": True}}, "not a count"),
        ({"agentic_rag": D1["agentic_rag"] | {"counts": [1]}}, "not a mapping"),
        ({"runbook_agent": D1["runbook_agent"] | {"scenarios": []}}, "scenarios"),
        ({"split": {"seed": 19}}, "split"),
        ({"queries": "../d1.queries.jsonl"}, "file name"),
        ({"exclude": 3}, "file name"),
    ],
)
def test_a_malformed_plan_file_is_refused(change: dict[str, Any], match: str) -> None:
    with pytest.raises(planfile.PlanFileError, match=match):
        planfile.normalize(D1 | change, "d1")


def test_a_split_needs_the_committed_query_list(tmp_path: Path) -> None:
    raw = {k: v for k, v in D1.items() if k != "queries"}
    with pytest.raises(planfile.PlanFileError, match="committed query list"):
        planfile.normalize(raw, "d1")
    path = tmp_path / "bad.yaml"
    path.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(planfile.PlanFileError, match="not a mapping"):
        planfile.load(path)
    path.write_text("seed: x\n", encoding="utf-8")
    with pytest.raises(planfile.PlanFileError, match=r"bad\.yaml: seed"):
        planfile.load(path)
