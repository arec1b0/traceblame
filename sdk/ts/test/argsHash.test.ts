// Decoded arguments: serialized like Python's json.dumps(ensure_ascii=True), so the raw-bytes
// fallback hashes the same bytes in both shims (ADR-0006 section 6). The expected strings and
// hashes were computed with traceblame-py, not with this code.
import { describe, expect, it } from "vitest";
import { argsHash, pythonDumps } from "../src/argsHash.js";

const CASES: [string, unknown, string, string][] = [
  ["flat", { deployment: "checkout-api", namespace: "prod" },
    '{"deployment": "checkout-api", "namespace": "prod"}', "a21ec7eb0c8ab1c3"],
  ["nested, non-ASCII, astral", { b: [1, 2.5, null, true], a: { "\u00e9": "\u{1f600}", x: "\x07\u2028" } },
    '{"b": [1, 2.5, null, true], "a": {"\\u00e9": "\\ud83d\\ude00", "x": "\\u0007\\u2028"}}', "46e34665b37db72f"],
  ["number forms", { n: 1e300, m: 1.5e-5, k: 0.1, big: 12345678901234, neg: -3 },
    '{"n": 1e+300, "m": 1.5e-05, "k": 0.1, "big": 12345678901234, "neg": -3}', "0696d9616e44d028"],
  ["lone surrogate (not I-JSON: raw bytes)", { lone: "\ud83d", z: "caf\u00e9" },
    '{"lone": "\\ud83d", "z": "caf\\u00e9"}', "c51278b1fbe27991"],
  ["non-finite numbers (not I-JSON: raw bytes)", { nan: NaN, inf: Infinity, ninf: -Infinity },
    '{"nan": NaN, "inf": Infinity, "ninf": -Infinity}', "64c42a6db99bb0f0"],
  ["array, escapes, integral numbers", [{ q: 'a"b\\c\n' }, 1e16, 1e15, 123456.789],
    '[{"q": "a\\"b\\\\c\\n"}, 1e+16, 1000000000000000, 123456.789]', "afcfd23440a77cde"],
];

describe("objects", () => {
  it.each(CASES)("%s", (_name, value, dumps, hash) => {
    expect(pythonDumps(value)).toBe(dumps);
    expect(argsHash(value as object)).toBe(hash);
  });

  it("text and decoded forms of I-JSON hash alike", () => {
    expect(argsHash('{"namespace":"prod","deployment":"checkout-api"}')).toBe("a21ec7eb0c8ab1c3");
  });

  it("skips undefined members like JSON.stringify, and writes undefined items as null", () => {
    expect(pythonDumps({ a: undefined, b: [undefined, 1] })).toBe('{"b": [null, 1]}');
  });

  it("uses toJSON where a value defines it", () => {
    expect(pythonDumps({ when: new Date(0) })).toBe('{"when": "1970-01-01T00:00:00.000Z"}');
  });

  it.each([
    ["a bigint", { n: 10n ** 20n }, '{"n": 100000000000000000000}'],
    ["negative zero, as Python's int 0", { z: -0 }, '{"z": 0}'],
  ])("%s", (_name, value, dumps) => {
    expect(pythonDumps(value)).toBe(dumps);
  });

  it.each([
    ["a function", { f: () => 1 }],
    ["a Map", { m: new Map() }],
    ["a symbol", Symbol("s")],
    ["a cycle", (() => { const o: Record<string, unknown> = {}; o["self"] = o; return o; })()],
  ])("refuses %s, which Python's json.dumps cannot serialize either", (_name, value) => {
    expect(() => pythonDumps(value)).toThrow(TypeError);
  });

  it("hashes raw text with lone surrogates as U+FFFD, like a TextEncoder", () => {
    // traceblame-py: args_hash('\ud83dx') replaces the lone surrogate the same way.
    expect(argsHash("\ud83dx")).toBe(argsHash("\ufffdx"));
  });
});
