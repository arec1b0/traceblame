"""Semantic diff between two revisions of the telemetry profile (ADR-0001, section 2).

Every change is classified:
- major: breaks producers or consumers of the old revision, so it needs the next profile
  major. An attribute removed, renamed or retyped (optional or not), an enum value removed,
  a requirement tightened to required/conditionally_required, a constraint added, a newly
  forbidden attribute, a `source.algorithm` added, removed or changed (ADR-0002, section 5).
  Producers and the normalizer must agree on an algorithm byte for byte, and hashes already
  stored follow the old one; only whitespace and line wrapping in its text are ignored.
- minor: backward compatible, so it is allowed within a major. An optional attribute or
  enum value added, a requirement loosened, a constraint removed, and a semconv pin change.

A pin change is minor by itself. It makes the diff major only through what it does to the
profile's attributes, which the semconv snapshot contract test keeps in sync with the pin.
"""

from dataclasses import dataclass
from typing import Literal

from traceblame_contracts.profile import AegisSource, AttributeSpec, Profile, RequirementLevel

Severity = Literal["major", "minor"]
Bump = Literal["major", "minor", "none"]

_STRENGTH: dict[RequirementLevel | None, int] = {
    None: 0,
    "opt_in": 1,
    "recommended": 2,
    "conditionally_required": 3,
    "required": 4,
}
_ENFORCED: set[RequirementLevel | None] = {"conditionally_required", "required"}


@dataclass(frozen=True)
class Change:
    severity: Severity
    rule: str
    subject: str
    detail: str = ""


@dataclass(frozen=True)
class ProfileDiff:
    changes: tuple[Change, ...]

    @property
    def breaking(self) -> list[Change]:
        return [c for c in self.changes if c.severity == "major"]

    @property
    def bump(self) -> Bump:
        """The smallest version bump that covers every change."""
        if self.breaking:
            return "major"
        return "minor" if self.changes else "none"


def diff_profiles(old: Profile, new: Profile) -> ProfileDiff:
    return ProfileDiff(
        (
            *_pin_changes(old, new),
            *_span_kind_changes(old, new),
            *_attribute_changes(old, new),
            *_forbidden_changes(old, new),
        )
    )


def breaking_within_major(old: Profile, new: Profile) -> list[Change]:
    """Major changes that `new` makes without moving to the next major (the CI gate)."""
    return diff_profiles(old, new).breaking if new.major == old.major else []


def _pin_changes(old: Profile, new: Profile) -> list[Change]:
    if old.semconv == new.semconv:
        return []
    detail = f"{old.semconv.version} -> {new.semconv.version}"
    if old.semconv.repository != new.semconv.repository:
        detail += f" ({old.semconv.repository} -> {new.semconv.repository})"
    return [Change("minor", "semconv_pin_changed", "semconv", detail)]


def _span_kind_changes(old: Profile, new: Profile) -> list[Change]:
    changes: list[Change] = []
    for kind, spec in old.span_kinds.items():
        new_spec = new.span_kinds.get(kind)
        if new_spec is None:
            changes.append(Change("major", "span_kind_removed", kind))
            continue
        before, after = set(spec.operations), set(new_spec.operations)
        if not before and after:
            changes.append(
                Change("major", "operations_constrained", kind, ", ".join(sorted(after)))
            )
        elif before and not after:
            changes.append(Change("minor", "operations_unconstrained", kind))
        else:
            if removed := sorted(before - after):
                changes.append(Change("major", "operation_removed", kind, ", ".join(removed)))
            if added := sorted(after - before):
                changes.append(Change("minor", "operation_added", kind, ", ".join(added)))
    changes += [
        Change("minor", "span_kind_added", kind)
        for kind in new.span_kinds
        if kind not in old.span_kinds
    ]
    return changes


def _attribute_changes(old: Profile, new: Profile) -> list[Change]:
    kinds = list(dict.fromkeys([*old.span_kinds, *new.span_kinds]))
    changes: list[Change] = []
    for a in old.attributes:
        b = new.attribute(a.name)
        if b is None:
            changes.append(Change("major", "attribute_removed", a.name))
            continue
        if a.type != b.type:
            changes.append(Change("major", "type_changed", a.name, f"{a.type} -> {b.type}"))
        if a.scope != b.scope:
            changes.append(Change("major", "scope_changed", a.name, f"{a.scope} -> {b.scope}"))
        changes += _requirement_changes(a, b, kinds)
        changes += _constraint_changes(a, b)
        changes += _algorithm_changes(a, b)
    for b in new.attributes:
        if old.attribute(b.name) is None:
            if any(b.level_for(kind) in _ENFORCED for kind in kinds):
                changes.append(Change("major", "required_attribute_added", b.name))
            else:
                changes.append(Change("minor", "attribute_added", b.name))
    return changes


def _requirement_changes(a: AttributeSpec, b: AttributeSpec, kinds: list[str]) -> list[Change]:
    changes: list[Change] = []
    for kind in kinds:
        before, after = a.level_for(kind), b.level_for(kind)
        if before == after:
            continue
        detail = f"{kind}: {before} -> {after}"
        if after in _ENFORCED and _STRENGTH[after] > _STRENGTH[before]:
            changes.append(Change("major", "requirement_tightened", a.name, detail))
        else:
            changes.append(Change("minor", "requirement_loosened", a.name, detail))
    return changes


def _constraint_changes(a: AttributeSpec, b: AttributeSpec) -> list[Change]:
    changes: list[Change] = []
    for name, before, after in (("pattern", a.pattern, b.pattern), ("const", a.const, b.const)):
        if before == after:
            continue
        if after is None:
            changes.append(Change("minor", "constraint_removed", a.name, f"{name} {before}"))
        else:
            changes.append(Change("major", "constraint_changed", a.name, f"{name} {after}"))
    if a.enum != b.enum:
        if b.enum is None:
            changes.append(Change("minor", "constraint_removed", a.name, "enum"))
        elif a.enum is None:
            changes.append(Change("major", "constraint_changed", a.name, "enum added"))
        else:
            if removed := sorted(set(a.enum) - set(b.enum)):
                changes.append(Change("major", "enum_value_removed", a.name, ", ".join(removed)))
            if added := sorted(set(b.enum) - set(a.enum)):
                changes.append(Change("minor", "enum_value_added", a.name, ", ".join(added)))
    return changes


def _algorithm(spec: AttributeSpec) -> str | None:
    """The algorithm text with whitespace runs collapsed, so wrapping is not a change."""
    text = spec.source.algorithm if isinstance(spec.source, AegisSource) else None
    return None if text is None else " ".join(text.split())


def _algorithm_changes(a: AttributeSpec, b: AttributeSpec) -> list[Change]:
    before, after = _algorithm(a), _algorithm(b)
    if before == after:
        return []
    if before is None:
        return [Change("major", "algorithm_added", a.name, after or "")]
    if after is None:
        return [Change("major", "algorithm_removed", a.name, before)]
    return [Change("major", "algorithm_changed", a.name, f"{before} -> {after}")]


def _forbidden_changes(old: Profile, new: Profile) -> list[Change]:
    before = {f.name for f in old.forbidden}
    after = {f.name for f in new.forbidden}
    return [
        *(Change("major", "forbidden_added", name) for name in sorted(after - before)),
        *(Change("minor", "forbidden_removed", name) for name in sorted(before - after)),
    ]
