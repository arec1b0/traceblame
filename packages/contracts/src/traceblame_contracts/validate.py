"""Validate flattened spans against the generated profile schema."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


@dataclass(frozen=True)
class Violation:
    path: str
    message: str


def validate_span(span: Mapping[str, Any], schema: Mapping[str, Any]) -> list[Violation]:
    """All schema violations of one span, ordered by their location in the document."""
    # jsonschema's stubs leave iter_errors partially unknown; the element type is fixed here.
    errors: Iterator[ValidationError] = Draft202012Validator(schema).iter_errors(span)  # pyright: ignore[reportUnknownMemberType]
    violations = [
        Violation("/".join(str(p) for p in error.absolute_path), error.message) for error in errors
    ]
    return sorted(violations, key=lambda v: (v.path, v.message))
