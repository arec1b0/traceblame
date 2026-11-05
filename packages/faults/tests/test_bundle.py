"""aegis.bundle.digest of a prompt variant: sha256 over the JCS form of the templates."""

import hashlib
import re

import pytest

from traceblame_faults import bundle
from traceblame_py._jcs import canonicalize  # pyright: ignore[reportPrivateUsage]

PROFILE_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def test_digest_is_in_the_profile_pattern() -> None:
    assert PROFILE_PATTERN.fullmatch(
        bundle.digest("agentic-rag", {"system": "s", "reasoning": "r"})
    )


def test_digest_is_sha256_over_jcs() -> None:
    templates = {"reasoning": 'Query: {query}\n"quoted"\tü', "system": "You are …"}
    want = hashlib.sha256(
        bundle.canonical({"app": "agentic-rag", "templates": templates}).encode()
    ).hexdigest()
    assert bundle.digest("agentic-rag", templates) == f"sha256:{want}"


@pytest.mark.parametrize(
    "obj",
    [
        {"b": "1", "a": {"z": '\u0000\u001f\b\f\n\r\t"\\/', "é": "x"}},
        {"\U0001f600": "a", "ﬁ": "b", "a": "c"},
        {},
    ],
)
def test_canonical_equals_the_shim_jcs(obj: dict[str, object]) -> None:
    import json

    assert bundle.canonical(obj) == canonicalize(json.dumps(obj))  # type: ignore[arg-type]


def test_canonical_refuses_non_strings() -> None:
    with pytest.raises(TypeError):
        bundle.canonical({"a": 1})  # type: ignore[dict-item]


def test_digest_changes_with_any_template_byte() -> None:
    assert bundle.digest("a", {"system": "x"}) != bundle.digest("a", {"system": "x "})
    assert bundle.digest("a", {"system": "x"}) != bundle.digest("b", {"system": "x"})
