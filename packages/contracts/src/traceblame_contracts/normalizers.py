"""The seam between golden fixtures and the profile schema in contract tests.

M0 ships only the identity normalizer, so raw SDK exports are checked as-is. The Go
normalizer processor (issue #6) plugs in here through a small CLI adapter.
"""

import copy
from collections.abc import Mapping
from typing import Any, Protocol


class Normalizer(Protocol):
    name: str

    def normalize(self, otlp: Mapping[str, Any]) -> dict[str, Any]:
        """Map one OTLP/JSON export to profile-conformant OTLP/JSON."""
        ...


class IdentityNormalizer:
    name = "identity"

    def normalize(self, otlp: Mapping[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(dict(otlp))


NORMALIZERS: tuple[Normalizer, ...] = (IdentityNormalizer(),)
