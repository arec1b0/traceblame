"""aegis.tool.args_hash, as the profile's source.algorithm defines it (algorithm_version 1).

First 16 lowercase hex characters of SHA-256 over the RFC 8785 (JCS) serialization of the
arguments; over their raw UTF-8 bytes when they are not I-JSON. Every vector in
schemas/profile/vectors/v1/aegis.tool.args_hash.json must match (tests/test_args_hash_vectors.py).
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from traceblame_py._jcs import NotIJSONError, canonicalize

ALGORITHM_VERSION = 1

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def args_hash(arguments: str | Mapping[str, Any] | Sequence[Any]) -> str:
    """Hash of tool call arguments, given as their text or as decoded JSON.

    Decoded arguments are serialized with ASCII escapes first, which JCS undoes for valid
    text. Raises TypeError when they are not JSON-serializable.
    """
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=True)
    try:
        data = canonicalize(text).encode("utf-8")
    except NotIJSONError:
        # A Python string may hold lone surrogates, which have no UTF-8 form; they become
        # U+FFFD, as a JavaScript TextEncoder does in the TypeScript shim (ADR-0006).
        data = _LONE_SURROGATE.sub("�", text).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]
