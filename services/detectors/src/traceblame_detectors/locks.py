"""Version locks (#15): a change to what decides a finding's bytes needs a new version.

- framework.lock.json pins, per FRAMEWORK_VERSION, the sha256 of the framework modules (engine,
  view, canonical, policy, context). Changing one without a new version and a new lock entry fails.
- plugins/versions.lock.json pins, per detector and code_version, the sha256 of the plugin's
  sources (plugins/<name>/*.py, tests excluded).

Both are append-only: an entry on the base revision never changes (tests/contract).
"""

import inspect
import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, cast

from traceblame_detectors.api import Detector
from traceblame_detectors.canonical import sha256_hex

FRAMEWORK_MODULES = ("canonical.py", "context.py", "engine.py", "policy.py", "view.py")
FRAMEWORK_LOCK = "framework.lock.json"
PLUGINS_LOCK = "plugins/versions.lock.json"


def _package_file(name: str) -> bytes:
    return (resources.files("traceblame_detectors") / name).read_bytes()


def framework_sources() -> dict[str, str]:
    return {name: sha256_hex(_package_file(name)) for name in FRAMEWORK_MODULES}


def load_lock(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(_package_file(name)))


def framework_problems(
    lock: Mapping[str, Any], version: str, sources: Mapping[str, str]
) -> list[str]:
    pinned = cast("Mapping[str, Mapping[str, str]]", lock.get("versions", {})).get(version)
    if pinned is None:
        return [f"FRAMEWORK_VERSION {version} has no entry in {FRAMEWORK_LOCK}"]
    return [
        f"{name} changed under FRAMEWORK_VERSION {version}: bump it and add a lock entry"
        for name in sorted(set(pinned) | set(sources))
        if pinned.get(name) != sources.get(name)
    ]


def plugin_sha(cls: type[Detector]) -> str:
    source = inspect.getsourcefile(cls)
    if source is None:
        raise ValueError(f"{cls.__name__}: no source file")
    files = sorted(p for p in Path(source).parent.glob("*.py"))
    return sha256_hex("".join(f"{p.name}:{sha256_hex(p.read_bytes())}\n" for p in files).encode())


def plugin_problems(
    registry: Mapping[str, type[Detector]], lock: Mapping[str, Mapping[str, str]]
) -> tuple[int, list[str]]:
    """(detectors checked, problems)."""
    problems: list[str] = []
    for name, cls in sorted(registry.items()):
        pinned = lock.get(name, {}).get(cls.spec.code_version)
        if pinned is None:
            problems.append(f"{name} {cls.spec.code_version}: no entry in {PLUGINS_LOCK}")
        elif pinned != plugin_sha(cls):
            problems.append(
                f"{name} changed under code_version {cls.spec.code_version}: "
                "bump it and add a lock entry"
            )
    problems.extend(
        f"{name}: locked but not registered" for name in sorted(set(lock) - set(registry))
    )
    return len(registry), problems


def append_only_problems(
    base: Mapping[str, Any], head: Mapping[str, Any], path: str = ""
) -> list[str]:
    """Every entry of the base lock is still in the head lock, unchanged."""
    problems: list[str] = []
    for key, value in base.items():
        where = f"{path}/{key}"
        if key not in head:
            problems.append(f"{where} was removed")
        elif isinstance(value, dict):
            problems.extend(
                append_only_problems(cast("Mapping[str, Any]", value), head[key], where)
            )
        elif head[key] != value:
            problems.append(f"{where} changed")
    return problems
