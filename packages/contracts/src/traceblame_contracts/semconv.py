"""Read the vendored OpenTelemetry semantic-conventions registry snapshot."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict


class SnapshotError(Exception):
    pass


class SnapshotSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    version: str
    tag: str
    commit: str
    schema_url: str
    license: str


@dataclass(frozen=True)
class SemconvAttribute:
    name: str
    type: str
    stability: str | None
    deprecated: bool
    members: tuple[object, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    root: Path
    source: SnapshotSource
    attributes: dict[str, SemconvAttribute]

    def checksum_mismatches(self) -> list[str]:
        """Files whose sha256 differs from SHA256SUMS (empty when the snapshot is intact)."""
        sums = self.root / "SHA256SUMS"
        if not sums.is_file():
            return ["SHA256SUMS is missing"]
        problems: list[str] = []
        listed: set[str] = set()
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, name = line.split(maxsplit=1)
            listed.add(name)
            path = self.root / name
            if not path.is_file():
                problems.append(f"{name}: missing")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                problems.append(f"{name}: checksum mismatch")
        on_disk = {
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and p.name != "SHA256SUMS"
        }
        problems += [f"{name}: not listed in SHA256SUMS" for name in sorted(on_disk - listed)]
        return problems


def _type_of(raw: object) -> tuple[str, tuple[object, ...]]:
    """Registry `type`: a plain name, or an enum given as `{members: [{value: ...}]}`."""
    if not isinstance(raw, dict):
        return str(raw), ()
    enum = cast("dict[str, list[dict[str, Any]]]", raw)
    values: tuple[object, ...] = tuple(m["value"] for m in enum["members"])
    kinds: set[type] = {type(v) for v in values}
    if kinds == {str}:
        return "string", values
    if kinds == {int}:
        return "int", values
    raise SnapshotError(f"unsupported enum member types: {sorted(k.__name__ for k in kinds)}")


def load_snapshot(root: Path) -> Snapshot:
    source = SnapshotSource.model_validate(
        yaml.safe_load((root / "source.yaml").read_text(encoding="utf-8"))
    )
    attributes: dict[str, SemconvAttribute] = {}
    for registry in sorted((root / "model").rglob("*.yaml")):
        data: dict[str, Any] = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
        for group in data.get("groups", []):
            for raw in group.get("attributes", []):
                if "id" not in raw:
                    continue  # a `ref:` to an attribute defined elsewhere
                name = raw["id"]
                if name in attributes:
                    raise SnapshotError(f"{name} is defined twice (again in {registry})")
                type_, members = _type_of(raw.get("type"))
                attributes[name] = SemconvAttribute(
                    name=name,
                    type=type_,
                    stability=raw.get("stability"),
                    deprecated="deprecated" in raw,
                    members=members,
                )
    return Snapshot(root=root, source=source, attributes=attributes)
