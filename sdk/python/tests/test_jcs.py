"""RFC 8785 (JCS) as the normalizer's internal/jcs implements it, on the RFC's own samples."""

import struct

import pytest

from traceblame_py._jcs import NotIJSONError, canonicalize, format_number

# RFC 8785 appendix B: IEEE 754 bit patterns and their canonical text.
_APPENDIX_B = [
    (0x0000000000000000, "0"),
    (0x8000000000000000, "0"),
    (0x0000000000000001, "5e-324"),
    (0x8000000000000001, "-5e-324"),
    (0x7FEFFFFFFFFFFFFF, "1.7976931348623157e+308"),
    (0xFFEFFFFFFFFFFFFF, "-1.7976931348623157e+308"),
    (0x4340000000000000, "9007199254740992"),
    (0xC340000000000000, "-9007199254740992"),
    (0x4430000000000000, "295147905179352830000"),
    (0x44B52D02C7E14AF5, "9.999999999999997e+22"),
    (0x44B52D02C7E14AF6, "1e+23"),
    (0x44B52D02C7E14AF7, "1.0000000000000001e+23"),
    (0x444B1AE4D6E2EF4E, "999999999999999700000"),
    (0x444B1AE4D6E2EF4F, "999999999999999900000"),
    (0x444B1AE4D6E2EF50, "1e+21"),
    (0x3EB0C6F7A0B5ED8C, "9.999999999999997e-7"),
    (0x3EB0C6F7A0B5ED8D, "0.000001"),
    (0x41B3DE4355555553, "333333333.3333332"),
    (0x41B3DE4355555554, "333333333.33333325"),
    (0x41B3DE4355555555, "333333333.3333333"),
    (0x41B3DE4355555556, "333333333.3333334"),
    (0x41B3DE4355555557, "333333333.33333343"),
    (0xBECBF647612F3696, "-0.0000033333333333333333"),
    (0x43143FF3C1CB0959, "1424953923781206.2"),
]


def _from_bits(bits: int) -> float:
    value: float = struct.unpack(">d", bits.to_bytes(8, "big"))[0]
    return value


@pytest.mark.parametrize(
    ("bits", "want"), _APPENDIX_B, ids=lambda v: f"{v:016x}" if isinstance(v, int) else v
)
def test_numbers_follow_appendix_b(bits: int, want: str) -> None:
    assert format_number(_from_bits(bits)) == want


@pytest.mark.parametrize("bits", [0x7FFFFFFFFFFFFFFF, 0x7FF0000000000000, 0xFFF0000000000000])
def test_nan_and_infinity_are_not_json(bits: int) -> None:
    with pytest.raises(NotIJSONError):
        format_number(_from_bits(bits))


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("[1e21, 1e20, 1e-7, 1e-6, 123e-9]", "[1e+21,100000000000000000000,1e-7,0.000001,1.23e-7]"),
        ("[9007199254740993]", "[9007199254740992]"),
        ("[-0, -0.0, 0e5]", "[0,0,0]"),
        ("[12345678901234567890123]", "[1.2345678901234568e+22]"),
        ("[1.50, 100, 10.0e1]", "[1.5,100,100]"),
    ],
)
def test_es6_number_edges(text: str, want: str) -> None:
    assert canonicalize(text) == want


def test_the_rfc_sample() -> None:
    text = (
        '{\n  "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],\n'
        '  "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",\n'
        '  "literals": [null, true, false]\n}'
    )
    assert canonicalize(text) == (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )


def test_properties_sort_by_utf16_code_units() -> None:
    text = '{"\\ufb33": 1, "\\ud83d\\ude00": 2, "\\u00f6": 3, "\\r": 4, "1": 5}'
    # U+1F600 is D83D DE00 in UTF-16, so it sorts before U+FB33 though its code point is larger.
    assert canonicalize(text) == '{"\\r":4,"1":5,"\u00f6":3,"\U0001f600":2,"\ufb33":1}'


def test_nested_objects_sort_and_arrays_keep_their_order() -> None:
    assert canonicalize('{"b": [3, {"z": 1, "a": 2}], "a": {}}') == '{"a":{},"b":[3,{"a":2,"z":1}]}'


def test_strings_escape_only_controls_quote_and_backslash() -> None:
    text = '["\\b\\f\\n\\r\\t\\u0001\\u001f \\u2028 \\u007f \\/"]'
    assert canonicalize(text) == '["\\b\\f\\n\\r\\t\\u0001\\u001f \u2028 \u007f /"]'


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1, "a": 2}',
        '{"a": "\\ud83d"}',
        '["\\ude00x"]',
        "[1e400]",
        "[-1e400]",
        "{} {}",
        '{"city":',
        "",
        "   ",
        "[NaN]",
        "[Infinity]",
        "{'a': 1}",
        "[01]",
        '["a\tb"]',
        "\ufeff[]",
    ],
)
def test_rejects_what_ijson_forbids(text: str) -> None:
    with pytest.raises(NotIJSONError):
        canonicalize(text)


def test_escaped_backslash_before_u_is_not_a_surrogate() -> None:
    assert canonicalize('["\\\\ud83d"]') == '["\\\\ud83d"]'


@pytest.mark.parametrize(
    ("text", "want"), [("null", "null"), ('"x"', '"x"'), (" 7 ", "7"), ("[]", "[]")]
)
def test_top_level_scalars(text: str, want: str) -> None:
    assert canonicalize(text) == want
