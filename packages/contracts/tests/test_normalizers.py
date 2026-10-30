from typing import Any

from traceblame_contracts.normalizers import NORMALIZERS, IdentityNormalizer


def test_identity_returns_an_equal_copy() -> None:
    doc: dict[str, Any] = {"resourceSpans": [{"scopeSpans": []}]}
    out = IdentityNormalizer().normalize(doc)
    assert out == doc
    out["resourceSpans"].clear()
    assert doc == {"resourceSpans": [{"scopeSpans": []}]}


def test_registry_names_are_unique() -> None:
    names = [n.name for n in NORMALIZERS]
    assert len(names) == len(set(names))
    assert "identity" in names
