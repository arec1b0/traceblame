/**
 * aegis.tool.args_hash, as the profile's source.algorithm defines it (algorithm_version 1).
 *
 * First 16 lowercase hex characters of SHA-256 over the RFC 8785 (JCS) serialization of the
 * arguments; over their raw UTF-8 bytes when they are not I-JSON. Every vector in
 * schemas/profile/vectors/v1/aegis.tool.args_hash.json must match (test/argsHashVectors.test.ts).
 */
import { createHash } from "node:crypto";
import { NotIJSONError, canonicalize } from "./jcs.js";

export const ALGORITHM_VERSION = 1;

/**
 * Hash of tool call arguments, given as their text or as decoded JSON.
 *
 * Decoded arguments are serialized like traceblame-py's (pythonDumps) first, which JCS undoes
 * for valid text. Throws TypeError when they are not JSON-serializable.
 */
export function argsHash(args: string | object): string {
  const text = typeof args === "string" ? args : pythonDumps(args);
  let data: Uint8Array;
  try {
    data = new TextEncoder().encode(canonicalize(text));
  } catch (error) {
    if (!(error instanceof NotIJSONError)) throw error;
    // A lone surrogate has no UTF-8 form: TextEncoder writes U+FFFD, as traceblame-py does.
    data = new TextEncoder().encode(text);
  }
  return createHash("sha256").update(data).digest("hex").slice(0, 16);
}

/**
 * The text Python's `json.dumps(value, ensure_ascii=True)` writes for the same value, so that
 * arguments that are not I-JSON (lone surrogates, NaN) hash the same bytes in both shims
 * (ADR-0006 section 6).
 *
 * JavaScript has one number type: a safe integer is written as Python writes an int, any other
 * number as Python writes a float (repr). Members whose value is undefined, a function or a
 * symbol are skipped as JSON.stringify skips them... except that Python cannot serialize a
 * function or symbol at all, so those throw TypeError, like a Map, a Set or a cycle.
 */
export function pythonDumps(value: unknown): string {
  return dump(value, new Set());
}

function dump(value: unknown, seen: Set<object>): string {
  if (value === null) return "null";
  switch (typeof value) {
    case "boolean":
      return value ? "true" : "false";
    case "number":
      return pythonNumber(value);
    case "bigint":
      return value.toString();
    case "string":
      return pythonString(value);
    case "object":
      break;
    default:
      throw new TypeError(`Object of type ${typeof value} is not JSON serializable`);
  }
  const withToJSON = value as { toJSON?: () => unknown };
  if (typeof withToJSON.toJSON === "function") return dump(withToJSON.toJSON(), seen);
  if (seen.has(value)) throw new TypeError("Circular reference detected");
  seen.add(value);
  try {
    if (Array.isArray(value)) {
      return "[" + value.map((item: unknown) => (item === undefined ? "null" : dump(item, seen))).join(", ") + "]";
    }
    const proto = Object.getPrototypeOf(value) as unknown;
    if (proto !== Object.prototype && proto !== null) {
      throw new TypeError(`Object of type ${value.constructor.name} is not JSON serializable`);
    }
    const members: string[] = [];
    for (const [key, item] of Object.entries(value)) {
      if (item === undefined) continue;
      members.push(`${pythonString(key)}: ${dump(item, seen)}`);
    }
    return "{" + members.join(", ") + "}";
  } finally {
    seen.delete(value);
  }
}

function pythonNumber(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (Number.isSafeInteger(value)) return String(Object.is(value, -0) ? 0 : value);
  // float.__repr__: the shortest round-trip digits (the same digits ECMAScript picks), in
  // fixed notation for decimal exponents -4..15, else d.ddde+XX with at least two digits.
  const [mantissa = "", exp = "0"] = value.toExponential().split("e");
  const e = Number(exp);
  const sign = mantissa.startsWith("-") ? "-" : "";
  const digits = mantissa.replace("-", "").replace(".", "");
  if (e >= -4 && e < 16) {
    let fixed: string;
    if (e < 0) {
      fixed = "0." + "0".repeat(-e - 1) + digits;
    } else if (digits.length > e + 1) {
      fixed = digits.slice(0, e + 1) + "." + digits.slice(e + 1);
    } else {
      fixed = digits + "0".repeat(e + 1 - digits.length) + ".0";
    }
    return sign + fixed;
  }
  const head = digits.length > 1 ? `${digits[0] ?? ""}.${digits.slice(1)}` : digits;
  const expSign = e < 0 ? "-" : "+";
  return `${sign}${head}e${expSign}${String(Math.abs(e)).padStart(2, "0")}`;
}

// ensure_ascii: everything outside printable ASCII becomes \uXXXX, per UTF-16 code unit.
function pythonString(value: string): string {
  let out = '"';
  for (let i = 0; i < value.length; i++) {
    const ch = value[i] as string;
    const code = value.charCodeAt(i);
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
    else out += ch;
  }
  return out + '"';
}
