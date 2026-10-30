"""Detect breaking changes between two revisions of the same profile major (ADR-0001)."""

from dataclasses import dataclass

from traceblame_contracts.profile import Profile, RequirementLevel

_STRENGTH: dict[RequirementLevel | None, int] = {
    None: 0,
    "opt_in": 1,
    "recommended": 2,
    "conditionally_required": 3,
    "required": 4,
}
_ENFORCED: set[RequirementLevel | None] = {"conditionally_required", "required"}


@dataclass(frozen=True)
class Break:
    rule: str
    subject: str
    detail: str


def diff_profiles(old: Profile, new: Profile) -> list[Break]:
    """Breaking changes from `old` to `new`. Different majors may break by definition."""
    if old.major != new.major:
        return []
    breaks: list[Break] = []
    if old.semconv != new.semconv:
        breaks.append(
            Break(
                "semconv_pin_changed",
                "semconv",
                f"{old.semconv.version} -> {new.semconv.version}",
            )
        )
    breaks += _span_kind_breaks(old, new)
    breaks += _attribute_breaks(old, new)
    old_forbidden = {f.name for f in old.forbidden}
    breaks += [
        Break("forbidden_added", f.name, f.reason)
        for f in new.forbidden
        if f.name not in old_forbidden
    ]
    return breaks


def _span_kind_breaks(old: Profile, new: Profile) -> list[Break]:
    breaks: list[Break] = []
    for kind, spec in old.span_kinds.items():
        new_spec = new.span_kinds.get(kind)
        if new_spec is None:
            breaks.append(Break("span_kind_removed", kind, ""))
        elif not spec.operations and new_spec.operations:
            breaks.append(Break("operations_constrained", kind, ", ".join(new_spec.operations)))
        elif removed := sorted(set(spec.operations) - set(new_spec.operations)):
            breaks.append(Break("operation_removed", kind, ", ".join(removed)))
    return breaks


def _attribute_breaks(old: Profile, new: Profile) -> list[Break]:
    breaks: list[Break] = []
    kinds = list(new.span_kinds)
    for a in old.attributes:
        b = new.attribute(a.name)
        if b is None:
            breaks.append(Break("attribute_removed", a.name, ""))
            continue
        if a.type != b.type:
            breaks.append(Break("type_changed", a.name, f"{a.type} -> {b.type}"))
        if a.scope != b.scope:
            breaks.append(Break("scope_changed", a.name, f"{a.scope} -> {b.scope}"))
        for kind in kinds:
            before, after = a.level_for(kind), b.level_for(kind)
            if after in _ENFORCED and _STRENGTH[after] > _STRENGTH[before]:
                breaks.append(
                    Break("requirement_tightened", a.name, f"{kind}: {before} -> {after}")
                )
        if b.pattern is not None and a.pattern != b.pattern:
            breaks.append(Break("constraint_changed", a.name, f"pattern {b.pattern}"))
        if b.const is not None and a.const != b.const:
            breaks.append(Break("constraint_changed", a.name, f"const {b.const}"))
        if b.enum is not None:
            if a.enum is None:
                breaks.append(Break("constraint_changed", a.name, "enum added"))
            elif removed := sorted(set(a.enum) - set(b.enum)):
                breaks.append(Break("enum_value_removed", a.name, ", ".join(removed)))
    for b in new.attributes:
        if old.attribute(b.name) is None and any(b.level_for(k) in _ENFORCED for k in kinds):
            breaks.append(Break("required_attribute_added", b.name, ""))
    return breaks
