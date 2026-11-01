"""Runner of the golden vectors of aegis.tool.args_hash (schemas/profile/vectors/README.md).

Every vector in the registry must produce its `expected` hash and its `canonical` form here,
and the registry's algorithm_version must be the one this shim implements.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from traceblame_py._jcs import NotIJSONError, canonicalize
from traceblame_py.args_hash import ALGORITHM_VERSION, args_hash

VECTORS = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "profile"
    / "vectors"
    / "v1"
    / "aegis.tool.args_hash.json"
)
_FILE: dict[str, Any] = json.loads(VECTORS.read_text(encoding="utf-8"))
_VECTORS: list[dict[str, Any]] = _FILE["vectors"]


def test_registry_is_for_the_algorithm_this_shim_implements() -> None:
    assert _FILE["attribute"] == "aegis.tool.args_hash"
    assert _FILE["algorithm_version"] == ALGORITHM_VERSION
    assert _VECTORS, "an empty registry would let every implementation pass"


@pytest.mark.parametrize("vector", _VECTORS, ids=lambda v: v["id"])
def test_vector(vector: dict[str, Any]) -> None:
    if vector["canonical"] is None:
        with pytest.raises(NotIJSONError):
            canonicalize(vector["input"])
    else:
        assert canonicalize(vector["input"]) == vector["canonical"]
    assert args_hash(vector["input"]) == vector["expected"]
