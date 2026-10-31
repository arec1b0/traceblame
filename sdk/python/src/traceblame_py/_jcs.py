"""RFC 8785, the JSON Canonicalization Scheme, as collector/processor/normalizer/internal/jcs.

The profile's aegis.tool.args_hash is computed over this form, so the shim and the normalizer
must produce the same bytes for the same arguments. Input must be I-JSON (RFC 7493): no
duplicate property names, no lone surrogates, numbers within the IEEE 754 double range, one
value and nothing after it. Anything else raises NotIJSONError.

Known divergence: Python's parser gives up below about 1000 levels of nesting, where Go's
gives up at 10000. Arguments nested that deep fall back to raw bytes here and not there.
"""

import json
import math
from decimal import Decimal
from typing import Any


class NotIJSONError(ValueError):
    """The text is not one I-JSON value."""


def canonicalize(text: str) -> str:
    """The RFC 8785 serialization of one JSON document."""
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_number,
            parse_int=_number,
        )
    except NotIJSONError:
        raise
    except (ValueError, RecursionError) as exc:
        raise NotIJSONError(str(exc)) from exc
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise NotIJSONError("duplicate property name")
        out[key] = value
    return out


def _constant(name: str) -> Any:
    raise NotIJSONError(f"{name} is not JSON")


def _number(literal: str) -> float:
    value = float(literal)
    if math.isinf(value):
        raise NotIJSONError("number out of the IEEE 754 double range")
    return value


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, float):
        out.append(format_number(value))
    elif isinstance(value, str):
        _write_string(value, out)
    elif isinstance(value, list):
        out.append("[")
        for i, item in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        obj: dict[str, Any] = value
        out.append("{")
        for i, key in enumerate(sorted(obj, key=_utf16)):
            if i:
                out.append(",")
            _write_string(key, out)
            out.append(":")
            _write(obj[key], out)
        out.append("}")


def _utf16(key: str) -> bytes:
    # Big-endian UTF-16 bytes compare like the code units RFC 8785 section 3.2.3 sorts by.
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise NotIJSONError("lone surrogate") from exc


_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _write_string(s: str, out: list[str]) -> None:
    out.append('"')
    for ch in s:
        code = ord(ch)
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise NotIJSONError("lone surrogate")
        else:
            out.append(ch)
    out.append('"')


def format_number(value: float) -> str:
    """ECMAScript Number.prototype.toString, which RFC 8785 section 3.2.2.3 prescribes."""
    if math.isnan(value) or math.isinf(value):
        raise NotIJSONError("NaN and Infinity are not JSON")
    if value == 0:
        return "0"
    if value < 0:
        return "-" + format_number(-value)
    # repr is the shortest text that round-trips, which is the digit string ECMAScript picks.
    _, digit_tuple, exponent = Decimal(repr(value)).as_tuple()
    if not isinstance(exponent, int):  # 'n', 'N' or 'F': excluded above
        raise NotIJSONError("NaN and Infinity are not JSON")
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    k = len(digits)
    # value = 0.digits * 10^n
    n = exponent + len(digit_tuple)
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * -n + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{mantissa}e{'+' if e >= 0 else '-'}{abs(e)}"
