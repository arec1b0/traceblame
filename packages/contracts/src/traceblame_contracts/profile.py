"""The telemetry profile: the YAML contract for normalized spans (ADR-0001)."""

import re
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILE_VERSION_ATTRIBUTE = "aegis.profile.version"
SPAN_KIND_ATTRIBUTE = "aegis.span.kind"
OPERATION_ATTRIBUTE = "gen_ai.operation.name"
AEGIS_PREFIX = "aegis."
ALL_KINDS = "all"

RequirementLevel = Literal["required", "conditionally_required", "recommended", "opt_in"]
AttributeType = Literal[
    "string", "int", "double", "boolean", "string[]", "int[]", "double[]", "boolean[]"
]

_SEMVER = r"^\d+\.\d+\.\d+$"
_ATTRIBUTE_NAME = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemconvPin(_Model):
    repository: str
    version: str = Field(pattern=_SEMVER)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    schema_url: str


class SpanKind(_Model):
    description: str
    operations: tuple[str, ...] = ()


class SemconvSource(_Model):
    semconv: str


class AegisSource(_Model):
    aegis: Literal["extension"]
    derived_from: tuple[str, ...] = ()
    algorithm: str | None = None
    # Names the computation `algorithm` describes, so a pure rewording can be told from a
    # change (ADR-0002, section 5). Optional and not read by any consumer.
    algorithm_version: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.algorithm_version is not None and self.algorithm is None:
            raise ValueError("algorithm_version needs an algorithm")
        return self


class AttributeSpec(_Model):
    name: str = Field(pattern=_ATTRIBUTE_NAME)
    scope: Literal["resource", "span"]
    type: AttributeType
    brief: str
    source: SemconvSource | AegisSource
    requirement: dict[str, RequirementLevel] = Field(min_length=1)
    condition: str | None = None
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    const: str | None = None

    @property
    def is_aegis(self) -> bool:
        return self.name.startswith(AEGIS_PREFIX)

    def level_for(self, kind: str) -> RequirementLevel | None:
        """Requirement level on spans of `kind`; a kind-specific entry overrides `all`."""
        level = self.requirement.get(kind)
        return level if level is not None else self.requirement.get(ALL_KINDS)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if isinstance(self.source, AegisSource) != self.is_aegis:
            raise ValueError(
                f"{self.name}: only aegis.* attributes may (and must) use an aegis source"
            )
        if isinstance(self.source, SemconvSource) and self.source.semconv != self.name:
            raise ValueError(
                f"{self.name}: semconv attributes keep their name; renaming "
                f"{self.source.semconv} belongs in normalizer mapping tables"
            )
        if self.scope == "resource" and set(self.requirement) != {ALL_KINDS}:
            raise ValueError(f"{self.name}: resource attributes apply to all span kinds")
        conditional = "conditionally_required" in self.requirement.values()
        if conditional != (self.condition is not None):
            raise ValueError(
                f"{self.name}: a condition is required with conditionally_required, "
                "and allowed only with it"
            )
        constraints = [c for c in (self.pattern, self.enum, self.const) if c is not None]
        if constraints and self.type != "string":
            raise ValueError(f"{self.name}: pattern/enum/const apply to string attributes only")
        if len(constraints) > 1:
            raise ValueError(f"{self.name}: use at most one of pattern, enum, const")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"{self.name}: invalid pattern: {exc}") from exc
        return self


class ForbiddenAttribute(_Model):
    name: str = Field(pattern=_ATTRIBUTE_NAME)
    reason: str


class Profile(_Model):
    profile: Literal["aegis-telemetry"]
    major: int = Field(ge=1)
    revision: str = Field(pattern=_SEMVER)
    semconv: SemconvPin
    span_kinds: dict[str, SpanKind] = Field(min_length=1)
    attributes: tuple[AttributeSpec, ...]
    forbidden: tuple[ForbiddenAttribute, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.profile}/{self.major}"

    def attribute(self, name: str) -> AttributeSpec | None:
        return next((a for a in self.attributes if a.name == name), None)

    def scoped(self, scope: Literal["resource", "span"]) -> list[AttributeSpec]:
        return [a for a in self.attributes if a.scope == scope]

    @model_validator(mode="after")
    def _check(self) -> Self:
        names = [a.name for a in self.attributes]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate attributes: {', '.join(duplicates)}")
        both = sorted(set(names) & {f.name for f in self.forbidden})
        if both:
            raise ValueError(f"attributes also listed as forbidden: {', '.join(both)}")
        if ALL_KINDS in self.span_kinds:
            raise ValueError(f"'{ALL_KINDS}' is reserved and cannot be a span kind")
        for attr in self.attributes:
            unknown = sorted(set(attr.requirement) - set(self.span_kinds) - {ALL_KINDS})
            if unknown:
                raise ValueError(f"{attr.name}: unknown span kinds {', '.join(unknown)}")
        self._check_profile_version()
        self._check_span_kind()
        if any(k.operations for k in self.span_kinds.values()):
            op = self.attribute(OPERATION_ATTRIBUTE)
            if op is None or op.scope != "span":
                raise ValueError(f"span kinds list operations, so {OPERATION_ATTRIBUTE} is needed")
        return self

    def _check_profile_version(self) -> None:
        attr = self.attribute(PROFILE_VERSION_ATTRIBUTE)
        if (
            attr is None
            or attr.scope != "span"
            or attr.const != self.id
            or attr.requirement != {ALL_KINDS: "required"}
        ):
            raise ValueError(
                f"{PROFILE_VERSION_ATTRIBUTE} must be a span attribute required on all kinds "
                f"with const {self.id!r}"
            )

    def _check_span_kind(self) -> None:
        attr = self.attribute(SPAN_KIND_ATTRIBUTE)
        if (
            attr is None
            or attr.scope != "span"
            or attr.enum is None
            or set(attr.enum) != set(self.span_kinds)
            or attr.requirement != {ALL_KINDS: "required"}
        ):
            raise ValueError(
                f"{SPAN_KIND_ATTRIBUTE} must be a span attribute required on all kinds "
                "whose enum lists exactly the span kinds"
            )


def load_profile(path: Path) -> Profile:
    with path.open(encoding="utf-8") as fh:
        return Profile.model_validate(yaml.safe_load(fh))
