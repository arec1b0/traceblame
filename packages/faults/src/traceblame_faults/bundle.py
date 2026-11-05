"""aegis.bundle.digest for a prompt variant, without promptops.

digest = "sha256:" + sha256(JCS({"app": <app>, "templates": {<name>: <template text>}}))

The input is the app's prompt templates (code, not request content), so this is a bundle
identity, not a content digest (ADR-0002 §2 forbids digests of prompts or completions). The
text itself never leaves the process: only the digest goes on spans and into params. The
TypeScript preload computes the same value with the shim's canonicalize (vectors/mutations.json).
"""

import hashlib
from collections.abc import Mapping
from typing import Any, cast

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16(key: str) -> bytes:
    return key.encode("utf-16-be", "surrogatepass")


def canonical(obj: Mapping[str, Any]) -> str:
    """RFC 8785 (JCS) for objects whose leaves are strings: keys sorted by UTF-16 code units."""
    parts: list[str] = []
    for key in sorted(obj, key=_utf16):
        value = obj[key]
        if isinstance(value, str):
            rendered = _string(value)
        elif isinstance(value, Mapping):
            rendered = canonical(cast("Mapping[str, Any]", value))
        else:
            raise TypeError(f"canonical: {key!r} is {type(value).__name__}, not str or object")
        parts.append(f"{_string(key)}:{rendered}")
    return "{" + ",".join(parts) + "}"


def digest(app: str, templates: Mapping[str, str]) -> str:
    text = canonical({"app": app, "templates": dict(templates)})
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()
