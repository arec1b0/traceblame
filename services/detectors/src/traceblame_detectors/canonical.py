"""The canonical JSON form: sorted keys, no whitespace, ASCII, no NaN or infinity.

Finding bytes, digests and params digests are computed over this form, so it is part of
FRAMEWORK_VERSION (framework.lock.json).
"""

import hashlib
import json


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: object) -> str:
    """sha256 of the canonical JSON form of a value."""
    return sha256_hex(canonical_json(value))
