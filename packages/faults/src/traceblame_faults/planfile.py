"""Committed plan files of a fault run (#70): tools/faults/plans/<name>.yaml, and the #14
tools/faults/validation.yaml in its own shape. Nothing is edited before a run; the drivers expand
a plan file with plan.plan(), a pure function of its seed.

  seed            the plan's seed
  agentic_rag     n, ratio (cuad.sample), queries (the first that many mapped answerable ones),
                  counts {kind: traces}, clean
  runbook_agent   scenarios, per_scenario, distinct (tasks with distinct (scenario, parameters)
                  tuples), counts {kind: traces}, clean, approval_timeout_ms
  exclude         a committed list of query refs this plan may not use (file name)
  queries         the plan's committed query list with each query's split side (file name);
                  the drivers recompute it and refuse a difference
  split           seed, max_kind_deviation (split.py): half the queries on each side
  loop, settle_seconds, assemble_seconds, orphan_slack_seconds: thresholds of the check

The #14 shape has no counts: every kind and a clean run on each of `queries` queries
(agentic_rag), and the kinds as keys (runbook_agent). normalize() gives both one shape.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

from traceblame_faults import kinds

APPS = {"agentic_rag": "agentic-rag", "runbook_agent": "runbook-agent"}
_RUNBOOK_KINDS = ("malformed_output", "prompt_variant")


class PlanFileError(ValueError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PlanFileError(f"{path}: not a mapping")
    try:
        return normalize(cast("dict[str, Any]", raw), path.stem)
    except PlanFileError as exc:
        raise PlanFileError(f"{path}: {exc}") from exc


def _count(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlanFileError(f"{what}: {value!r} is not a count")
    return value


def normalize(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    out: dict[str, Any] = dict(raw)
    out["name"] = str(raw.get("name", name))
    out["seed"] = _count(raw.get("seed"), "seed")
    for key, app in APPS.items():
        if not isinstance(raw.get(key), dict):
            raise PlanFileError(f"{key}: missing")
        section: dict[str, Any] = dict(raw[key])
        if "counts" not in section:
            if key == "agentic_rag":
                n = _count(section.get("queries"), f"{key}.queries")
                section["counts"] = {k: n for k, v in sorted(kinds.KINDS.items()) if app in v.apps}
                section["clean"] = n
            else:
                section["counts"] = {k: section.get(k, 0) for k in _RUNBOOK_KINDS}
        counts = section["counts"]
        if not isinstance(counts, dict):
            raise PlanFileError(f"{key}.counts: not a mapping")
        for kind, n in cast("dict[str, Any]", counts).items():
            if kind not in kinds.KINDS or app not in kinds.KINDS[kind].apps:
                raise PlanFileError(f"{key}.counts: {kind!r} is not a fault kind of {app}")
            _count(n, f"{key}.counts.{kind}")
        section["clean"] = _count(section.get("clean"), f"{key}.clean")
        out[key] = section
    rb = out["runbook_agent"]
    rb["distinct"] = bool(rb.get("distinct", False))
    _count(rb.get("per_scenario"), "runbook_agent.per_scenario")
    if not isinstance(rb.get("scenarios"), list) or not rb["scenarios"]:
        raise PlanFileError("runbook_agent.scenarios: missing")
    split = raw.get("split")
    if split is not None:
        if not isinstance(split, dict) or set(cast("dict[str, Any]", split)) != {
            "seed",
            "max_kind_deviation",
        }:
            raise PlanFileError("split: want seed and max_kind_deviation")
        _count(split["seed"], "split.seed")
        _count(split["max_kind_deviation"], "split.max_kind_deviation")
        if "queries" not in raw:
            raise PlanFileError("split: needs the committed query list (queries)")
    for key in ("queries", "exclude"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or "/" in value):
            raise PlanFileError(f"{key}: a file name next to the plan file")
    return out


def composition(cfg: Mapping[str, Any]) -> dict[tuple[str, str], int]:
    """(app, kind | "clean") -> planned traces, in the plan file's order."""
    out: dict[tuple[str, str], int] = {}
    for key, app in APPS.items():
        for kind, n in cfg[key]["counts"].items():
            out[app, str(kind)] = int(n)
        out[app, "clean"] = int(cfg[key]["clean"])
    return out
