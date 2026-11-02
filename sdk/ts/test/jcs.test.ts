// RFC 8785 (JCS) as the normalizer's internal/jcs and traceblame-py implement it, on the RFC's
// own samples. A port of sdk/python/tests/test_jcs.py.
import { describe, expect, it } from "vitest";
import { NotIJSONError, canonicalize, formatNumber } from "../src/jcs.js";

// RFC 8785 appendix B: IEEE 754 bit patterns and their canonical text.
const APPENDIX_B: [string, string][] = [
  ["0000000000000000", "0"],
  ["8000000000000000", "0"],
  ["0000000000000001", "5e-324"],
  ["8000000000000001", "-5e-324"],
  ["7fefffffffffffff", "1.7976931348623157e+308"],
  ["ffefffffffffffff", "-1.7976931348623157e+308"],
  ["4340000000000000", "9007199254740992"],
  ["c340000000000000", "-9007199254740992"],
  ["4430000000000000", "295147905179352830000"],
  ["44b52d02c7e14af5", "9.999999999999997e+22"],
  ["44b52d02c7e14af6", "1e+23"],
  ["44b52d02c7e14af7", "1.0000000000000001e+23"],
  ["444b1ae4d6e2ef4e", "999999999999999700000"],
  ["444b1ae4d6e2ef4f", "999999999999999900000"],
  ["444b1ae4d6e2ef50", "1e+21"],
  ["3eb0c6f7a0b5ed8c", "9.999999999999997e-7"],
  ["3eb0c6f7a0b5ed8d", "0.000001"],
  ["41b3de4355555553", "333333333.3333332"],
  ["41b3de4355555554", "333333333.33333325"],
  ["41b3de4355555555", "333333333.3333333"],
  ["41b3de4355555556", "333333333.3333334"],
  ["41b3de4355555557", "333333333.33333343"],
  ["becbf647612f3696", "-0.0000033333333333333333"],
  ["43143ff3c1cb0959", "1424953923781206.2"],
];

function fromBits(hex: string): number {
  return Buffer.from(hex, "hex").readDoubleBE(0);
}

describe("numbers", () => {
  it.each(APPENDIX_B)("appendix B %s", (bits, want) => {
    expect(formatNumber(fromBits(bits))).toBe(want);
  });

  it.each(["7fffffffffffffff", "7ff0000000000000", "fff0000000000000"])(
    "NaN and infinity are not JSON (%s)",
    (bits) => {
      expect(() => formatNumber(fromBits(bits))).toThrow(NotIJSONError);
    },
  );

  it.each([
    ["[1e21, 1e20, 1e-7, 1e-6, 123e-9]", "[1e+21,100000000000000000000,1e-7,0.000001,1.23e-7]"],
    ["[9007199254740993]", "[9007199254740992]"],
    ["[-0, -0.0, 0e5]", "[0,0,0]"],
    ["[12345678901234567890123]", "[1.2345678901234568e+22]"],
    ["[1.50, 100, 10.0e1]", "[1.5,100,100]"],
  ])("ES6 edges %s", (text, want) => {
    expect(canonicalize(text)).toBe(want);
  });
});

describe("serialization", () => {
  it("the RFC sample", () => {
    const text =
      '{\n  "numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],\n' +
      '  "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",\n' +
      '  "literals": [null, true, false]\n}';
    expect(canonicalize(text)).toBe(
      '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],' +
        '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}',
    );
  });

  it("properties sort by UTF-16 code units", () => {
    const text = '{"\\ufb33": 1, "\\ud83d\\ude00": 2, "\\u00f6": 3, "\\r": 4, "1": 5}';
    // U+1F600 is D83D DE00 in UTF-16, so it sorts before U+FB33 though its code point is larger.
    expect(canonicalize(text)).toBe('{"\\r":4,"1":5,"\u00f6":3,"\u{1f600}":2,"\ufb33":1}');
  });

  it("nested objects sort and arrays keep their order", () => {
    expect(canonicalize('{"b": [3, {"z": 1, "a": 2}], "a": {}}')).toBe(
      '{"a":{},"b":[3,{"a":2,"z":1}]}',
    );
  });

  it("strings escape only controls, quote and backslash", () => {
    const text = '["\\b\\f\\n\\r\\t\\u0001\\u001f \\u2028 \\u007f \\/"]';
    expect(canonicalize(text)).toBe('["\\b\\f\\n\\r\\t\\u0001\\u001f \u2028 \u007f /"]');
  });

  it("an escaped backslash before u is not a surrogate", () => {
    expect(canonicalize('["\\\\ud83d"]')).toBe('["\\\\ud83d"]');
  });

  it.each([
    ["null", "null"],
    ['"x"', '"x"'],
    [" 7 ", "7"],
    ["[]", "[]"],
  ])("top-level scalar %s", (text, want) => {
    expect(canonicalize(text)).toBe(want);
  });
});

describe("I-JSON", () => {
  it.each([
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
    // Beyond what the Python port lists: grammar corners a hand-written parser must refuse.
    "[1,]",
    '{"a":1,}',
    "[.5]",
    "[1.]",
    "[1e]",
    "[-]",
    '["\\x"]',
    '["\\u12"]',
    "tru",
    "[1 2]",
    '"a" "b"',
    "\u00a0[]",
    '["\ud83d"]',
  ])("rejects %j", (text) => {
    expect(() => canonicalize(text)).toThrow(NotIJSONError);
  });

  it("refuses nesting beyond the normalizer's limit", () => {
    expect(() => canonicalize("[".repeat(10_001) + "]".repeat(10_001))).toThrow(NotIJSONError);
    expect(canonicalize("[".repeat(10_000) + "]".repeat(10_000))).toBe(
      "[".repeat(10_000) + "]".repeat(10_000),
    );
  });
});
