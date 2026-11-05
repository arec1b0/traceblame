"""Scenarios: one YAML file per (fault kind, app) under scenarios/, packaged with the library.

    scenario: <id>            # injections.params.scenario
    version: <int>            # bumped on any change to what the scenario draws
    kind: <fault kind>
    app: agentic-rag | runbook-agent
    description: <text>
    eligible: {strata: [...], golden: [...]}     # optional; omitted = any query
    params:                   # one generator per planned param of the kind
      <name>: {value: x} | {choice: [...]} | {int: [lo, hi]} | {float: [lo, hi]}
    variants:                 # prompt_variant only
      <name>: {templates: {<template>: <file under scenarios/>}, source: <text>, rationale: <text>}

A template file holds the template text and one final newline, which is not part of it.

Every value a generator can produce is checked against the kind's closed schema at load.
"""

import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from traceblame_faults import __version__, kinds

if TYPE_CHECKING:
    from traceblame_faults.plan import Query

_TOP = {"scenario", "version", "kind", "app", "description", "eligible", "params", "variants"}
_FLOAT_DIGITS = 2


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Variant:
    name: str
    templates: Mapping[str, str]  # template name -> text (never exported)
    source: str
    rationale: str


@dataclass(frozen=True)
class Scenario:
    name: str
    version: int
    kind: str
    app: str
    description: str
    params: Mapping[str, Mapping[str, Any]]
    strata: frozenset[str] | None = None
    golden: frozenset[str] | None = None
    variants: Mapping[str, Variant] = field(default_factory=dict[str, Variant])


def _values(name: str, gen: Mapping[str, Any]) -> list[Any]:
    """The values a generator can produce, or its bounds, for the schema check."""
    if len(gen) != 1:
        raise ScenarioError(f"param {name}: one generator, got {sorted(gen)}")
    (how, arg), *_ = gen.items()
    if how == "value":
        return [arg]
    if how == "choice" and isinstance(arg, list) and arg:
        return cast("list[Any]", arg)
    if how in ("int", "float") and isinstance(arg, list) and len(cast("list[Any]", arg)) == 2:
        lo, hi = cast("list[Any]", arg)
        if lo > hi:
            raise ScenarioError(f"param {name}: empty range {arg}")
        return [lo, hi] if how == "int" else [float(lo), float(hi)]
    raise ScenarioError(f"param {name}: unknown generator {gen!r}")


def _load_variants(raw: Any, base: Traversable, where: str) -> dict[str, Variant]:
    out: dict[str, Variant] = {}
    for name, spec in cast("dict[str, Any]", raw or {}).items():
        if set(spec) != {"templates", "source", "rationale"}:
            raise ScenarioError(f"{where}: variant {name}: templates, source, rationale")
        templates = {
            t: (base / f).read_text(encoding="utf-8").removesuffix("\n")
            for t, f in cast("dict[str, str]", spec["templates"]).items()
        }
        out[name] = Variant(name, templates, str(spec["source"]), str(spec["rationale"]))
    return out


def _parse(data: Any, base: Traversable, where: str) -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioError(f"{where}: not a mapping")
    doc = cast("dict[str, Any]", data)
    extra = set(doc) - _TOP
    if extra:
        raise ScenarioError(f"{where}: unknown keys {sorted(extra)}")
    kind, app = str(doc.get("kind")), str(doc.get("app"))
    if kind not in kinds.KINDS:
        raise ScenarioError(f"{where}: unknown fault kind {kind!r}")
    if app not in kinds.KINDS[kind].apps:
        raise ScenarioError(f"{where}: {kind} is not for {app}")
    params = cast("dict[str, dict[str, Any]]", doc.get("params") or {})
    schema = kinds.schema(kind)
    planned = {n for n, s in schema.items() if not s.applied_only} - set(kinds.COMMON)
    missing = planned - set(params)
    if missing:
        raise ScenarioError(f"{where}: no generator for {sorted(missing)}")
    try:
        for name, gen in params.items():
            if name in kinds.COMMON or schema.get(name, kinds.Param("id")).applied_only:
                raise ScenarioError(f"{where}: {name} is not drawn by a scenario")
            for value in _values(name, gen):
                kinds.check_param(kind, name, value)
    except kinds.ParamsError as exc:
        raise ScenarioError(f"{where}: {exc}") from exc
    eligible = cast("dict[str, list[str]]", doc.get("eligible") or {})
    variants = _load_variants(doc.get("variants"), base, where)
    if kind == "prompt_variant":
        choices = set(_values("variant", params["variant"]))
        if not variants or choices != set(variants):
            raise ScenarioError(f"{where}: variant choices {sorted(choices)} != variants")
    elif variants:
        raise ScenarioError(f"{where}: variants are for prompt_variant only")
    sc = Scenario(
        name=str(doc.get("scenario")),
        version=int(doc.get("version", 0)),
        kind=kind,
        app=app,
        description=str(doc.get("description", "")),
        params=params,
        strata=frozenset(eligible["strata"]) if "strata" in eligible else None,
        golden=frozenset(eligible["golden"]) if "golden" in eligible else None,
        variants=variants,
    )
    try:
        kinds.check_param(kind, "scenario", sc.name)
        kinds.check_param(kind, "scenario_version", sc.version)
    except kinds.ParamsError as exc:
        raise ScenarioError(f"{where}: {exc}") from exc
    return sc


def load(path: Path) -> Scenario:
    return _parse(yaml.safe_load(path.read_text(encoding="utf-8")), path.parent, str(path))


def packaged_dir() -> Traversable:
    return resources.files("traceblame_faults") / "scenarios"


def load_all(directory: Traversable | None = None) -> dict[tuple[str, str], Scenario]:
    """Every scenario, by (kind, app); two for one pair is an error."""
    base = directory or packaged_dir()
    out: dict[tuple[str, str], Scenario] = {}
    for f in sorted(base.iterdir(), key=lambda t: t.name):
        if not f.name.endswith(".yaml"):
            continue
        sc = _parse(yaml.safe_load(f.read_text(encoding="utf-8")), base, f.name)
        if (sc.kind, sc.app) in out:
            raise ScenarioError(f"{f.name}: a second scenario for {(sc.kind, sc.app)}")
        out[(sc.kind, sc.app)] = sc
    return out


def draw(sc: Scenario, rng: random.Random) -> dict[str, Any]:
    """The planned params of one entry, in a fixed order, from rng alone."""
    out: dict[str, Any] = {
        "scenario": sc.name,
        "scenario_version": sc.version,
        "faults_version": __version__,
    }
    for name in sorted(sc.params):
        (how, arg), *_ = sc.params[name].items()
        if how == "value":
            out[name] = arg
        elif how == "choice":
            out[name] = rng.choice(arg)
        elif how == "int":
            out[name] = rng.randint(int(arg[0]), int(arg[1]))
        else:
            out[name] = round(rng.uniform(float(arg[0]), float(arg[1])), _FLOAT_DIGITS)
    return out


def eligible(sc: Scenario, query: "Query") -> bool:
    return (
        query.app == sc.app
        and (sc.strata is None or query.stratum in sc.strata)
        and (sc.golden is None or query.golden_status in sc.golden)
    )
