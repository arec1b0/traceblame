"""Detector configuration: which detectors run, with which behaviour params (#15).

```yaml
mode: production          # or eval (offline runs only)
detectors:
  <name>:
    params: {<field>: <value>, ...}   # behaviour params: thresholds, windows, switches
worker: {...}             # operational settings (PR-B): never part of a detector version
offline: {...}            # operational settings of offline runs
```

A detector's version is `<code semver>+p<first 8 hex of sha256(canonical params)>`. The params
are the detector's params dataclass with every field resolved (defaults included), so leaving a
field out and writing its default give the same version; operational settings never enter it.
`mode: production` refuses eval_only detectors (CLAUDE.md rule 9).
"""

import dataclasses
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

from traceblame_detectors.api import NAME, SEMVER, Detector, DetectorSpec
from traceblame_detectors.canonical import canonical_json, sha256_hex
from traceblame_detectors.model import ContextNeed
from traceblame_detectors.policy import declarable

TOP_LEVEL = frozenset({"mode", "detectors", "worker", "offline"})
PARAM_TYPES: tuple[type, ...] = (bool, int, float, str)


class ConfigError(ValueError):
    pass


class Mode(StrEnum):
    PRODUCTION = "production"
    EVAL = "eval"


@dataclass(frozen=True)
class EnabledDetector:
    cls: type[Detector]
    params: Any
    params_json: str
    detector_version: str

    @property
    def spec(self) -> DetectorSpec:
        return self.cls.spec

    def instance(self) -> Detector:
        return self.cls(self.params)


@dataclass(frozen=True)
class DetectorConfig:
    mode: Mode
    detectors: tuple[EnabledDetector, ...]
    worker: Mapping[str, Any]
    offline: Mapping[str, Any]

    @property
    def attr_keys(self) -> frozenset[str]:
        """The union of the enabled detectors' declared attributes: what a source loads."""
        return frozenset(k for d in self.detectors for k in d.spec.reads.attrs)


def spec_problems(spec: DetectorSpec) -> list[str]:
    problems: list[str] = []
    if not NAME.fullmatch(spec.name):
        problems.append(f"name {spec.name!r} does not match {NAME.pattern}")
    if not SEMVER.fullmatch(spec.code_version):
        problems.append(f"code_version {spec.code_version!r} is not semver")
    if not spec.reads.span_kinds:
        problems.append("reads.span_kinds is empty")
    problems.extend(
        f"attribute {key!r} may not be declared (not a declarable profile attribute, or denied)"
        for key in sorted(spec.reads.attrs)
        if not declarable(key)
    )
    if ContextNeed.EVAL_GOLDEN in spec.needs and not spec.eval_only:
        problems.append("only an eval_only detector may need eval_golden")
    if not (dataclasses.is_dataclass(spec.params_type) and _frozen(spec.params_type)):
        problems.append("params_type must be a frozen dataclass")
    else:
        for name, kind in typing.get_type_hints(spec.params_type).items():
            if kind not in PARAM_TYPES:
                problems.append(f"param {name}: type {kind} is not one of bool, int, float, str")
    return [f"detector {spec.name}: {p}" for p in problems]


def _frozen(cls: type[Any]) -> bool:
    params = cast("Any", cls).__dataclass_params__
    return bool(params.frozen)


def resolve_params(spec: DetectorSpec, given: Mapping[str, Any]) -> tuple[Any, str]:
    """The params instance and its canonical JSON."""
    hints = typing.get_type_hints(spec.params_type)
    unknown = sorted(set(given) - set(hints))
    if unknown:
        raise ConfigError(f"detector {spec.name}: unknown params {unknown}")
    values: dict[str, Any] = {}
    for name, kind in hints.items():
        if name not in given:
            continue
        value = given[name]
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if type(value) is not kind:
            raise ConfigError(f"detector {spec.name}: param {name} must be {kind.__name__}")
        values[name] = value
    try:
        params = spec.params_type(**values)
    except TypeError as e:
        raise ConfigError(f"detector {spec.name}: {e}") from e
    resolved = {f.name: getattr(params, f.name) for f in dataclasses.fields(params)}
    return params, canonical_json(resolved).decode("ascii")


def detector_version(code_version: str, params_json: str) -> str:
    return f"{code_version}+p{sha256_hex(params_json.encode('ascii'))[:8]}"


def load_config(data: Mapping[str, Any], registry: Mapping[str, type[Detector]]) -> DetectorConfig:
    unknown = sorted(set(data) - TOP_LEVEL)
    if unknown:
        raise ConfigError(f"unknown top-level keys {unknown}")
    try:
        mode = Mode(data.get("mode", Mode.PRODUCTION.value))
    except ValueError as e:
        raise ConfigError(f"mode must be one of {[m.value for m in Mode]}") from e
    entries = cast("Mapping[str, Any]", data.get("detectors") or {})
    enabled: list[EnabledDetector] = []
    for name in sorted(entries):
        if name not in registry:
            raise ConfigError(f"detector {name}: not in the registry")
        cls = registry[name]
        spec = cls.spec
        if spec.name != name:
            raise ConfigError(f"detector {name}: registered under another name than its spec's")
        problems = spec_problems(spec)
        if problems:
            raise ConfigError("; ".join(problems))
        if spec.eval_only and mode is Mode.PRODUCTION:
            raise ConfigError(f"detector {name}: eval_only, refused by mode production")
        entry = cast("Mapping[str, Any]", entries[name] or {})
        extra = sorted(set(entry) - {"params"})
        if extra:
            raise ConfigError(f"detector {name}: unknown keys {extra}")
        params, params_json = resolve_params(
            spec, cast("Mapping[str, Any]", entry.get("params") or {})
        )
        enabled.append(
            EnabledDetector(
                cls, params, params_json, detector_version(spec.code_version, params_json)
            )
        )
    return DetectorConfig(
        mode=mode,
        detectors=tuple(enabled),
        worker=cast("Mapping[str, Any]", data.get("worker") or {}),
        offline=cast("Mapping[str, Any]", data.get("offline") or {}),
    )


def load_config_file(path: Path, registry: Mapping[str, type[Detector]]) -> DetectorConfig:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return load_config(cast("Mapping[str, Any]", data), registry)
