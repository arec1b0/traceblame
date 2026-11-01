/**
 * RFC 8785, the JSON Canonicalization Scheme, as collector/processor/normalizer/internal/jcs and
 * traceblame-py implement it.
 *
 * The profile's aegis.tool.args_hash is computed over this form, so the shims and the normalizer
 * must produce the same bytes for the same arguments. Input must be I-JSON (RFC 7493): no
 * duplicate property names, no lone surrogates, numbers within the IEEE 754 double range, one
 * value and nothing after it. Anything else throws NotIJSONError. `JSON.parse` cannot be used:
 * it keeps the last of duplicate properties silently.
 */

/** The text is not one I-JSON value. */
export class NotIJSONError extends Error {
  override name = "NotIJSONError";
}

// Go's encoding/json gives up at 10000 levels; so does this parser (traceblame-py stops near 1000).
const MAX_DEPTH = 10_000;

type Value = null | boolean | number | string | Value[] | Map<string, Value>;

/** The RFC 8785 serialization of one JSON document. */
export function canonicalize(text: string): string {
  const parser = new Parser(text);
  const value = parser.document();
  const out: string[] = [];
  write(value, out);
  return out.join("");
}

/** ECMAScript Number#toString, which is the RFC 8785 number form; -0 is 0. */
export function formatNumber(value: number): string {
  if (!Number.isFinite(value)) {
    throw new NotIJSONError(`${String(value)} is not JSON`);
  }
  return Object.is(value, -0) ? "0" : String(value);
}

// Iterative, like the parser: 10000 levels would overflow the call stack.
function write(root: Value, out: string[]): void {
  const todo: (Value | { token: string })[] = [root];
  while (todo.length > 0) {
    const value = todo.pop() as Value | { token: string };
    if (value === null) {
      out.push("null");
    } else if (typeof value === "boolean") {
      out.push(value ? "true" : "false");
    } else if (typeof value === "number") {
      out.push(formatNumber(value));
    } else if (typeof value === "string") {
      writeString(value, out);
    } else if (Array.isArray(value)) {
      out.push("[");
      todo.push({ token: "]" });
      for (let i = value.length - 1; i >= 0; i--) {
        todo.push(value[i] as Value);
        if (i) todo.push({ token: "," });
      }
    } else if (value instanceof Map) {
      out.push("{");
      todo.push({ token: "}" });
      // UTF-16 code unit order is JavaScript's own string order.
      const keys = [...value.keys()].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
      for (let i = keys.length - 1; i >= 0; i--) {
        const key = keys[i] as string;
        todo.push(value.get(key) as Value);
        const head: string[] = [];
        writeString(key, head);
        todo.push({ token: (i ? "," : "") + head.join("") + ":" });
      }
    } else {
      out.push(value.token);
    }
  }
}

const SHORT_ESCAPES: Record<string, string> = {
  "\b": "\\b",
  "\t": "\\t",
  "\n": "\\n",
  "\f": "\\f",
  "\r": "\\r",
  '"': '\\"',
  "\\": "\\\\",
};

function writeString(value: string, out: string[]): void {
  out.push('"');
  for (const ch of value) {
    const short = SHORT_ESCAPES[ch];
    if (short !== undefined) {
      out.push(short);
    } else if (ch < " ") {
      out.push("\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
    } else {
      out.push(ch);
    }
  }
  out.push('"');
}

const WHITESPACE = new Set([" ", "\t", "\n", "\r"]);
const NUMBER = /-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/y;
const ESCAPES: Record<string, string> = {
  '"': '"',
  "\\": "\\",
  "/": "/",
  b: "\b",
  f: "\f",
  n: "\n",
  r: "\r",
  t: "\t",
};

type Frame =
  | { kind: "array"; items: Value[] }
  | { kind: "object"; map: Map<string, Value>; key: string };

// An explicit stack instead of recursion, so the depth limit is the normalizer's (10000), not
// the call stack's.
class Parser {
  private pos = 0;

  constructor(private readonly text: string) {}

  document(): Value {
    const stack: Frame[] = [];
    let value: Value;
    this.skip();
    for (;;) {
      // Start a value; containers push a frame and either complete empty or start a member.
      const ch = this.text[this.pos];
      if (ch === "[" || ch === "{") {
        if (stack.length >= MAX_DEPTH) this.fail("nesting too deep");
        this.pos++;
        this.skip();
        if (ch === "[") {
          if (this.text[this.pos] === "]") {
            this.pos++;
            value = [];
          } else {
            stack.push({ kind: "array", items: [] });
            continue;
          }
        } else if (this.text[this.pos] === "}") {
          this.pos++;
          value = new Map();
        } else {
          stack.push({ kind: "object", map: new Map(), key: this.key(new Map()) });
          continue;
        }
      } else {
        value = this.scalar();
      }
      // Complete values upwards until one needs another member.
      for (;;) {
        const top = stack.at(-1);
        if (top === undefined) {
          this.skip();
          if (this.pos !== this.text.length) this.fail("trailing data");
          return value;
        }
        if (top.kind === "array") top.items.push(value);
        else top.map.set(top.key, value);
        this.skip();
        const next = this.text[this.pos++];
        const close = top.kind === "array" ? "]" : "}";
        if (next === ",") {
          this.skip();
          if (top.kind === "object") top.key = this.key(top.map);
          break;
        }
        if (next !== close) this.fail(`expected ',' or '${close}'`);
        stack.pop();
        value = top.kind === "array" ? top.items : top.map;
      }
    }
  }

  private fail(what: string): never {
    throw new NotIJSONError(`${what} at ${String(this.pos)}`);
  }

  private skip(): void {
    while (this.pos < this.text.length && WHITESPACE.has(this.text[this.pos] as string)) {
      this.pos++;
    }
  }

  // A property name and its colon; the value follows.
  private key(map: Map<string, Value>): string {
    if (this.text[this.pos] !== '"') this.fail("expected a property name");
    const key = this.string();
    if (map.has(key)) this.fail("duplicate property name");
    this.skip();
    if (this.text[this.pos] !== ":") this.fail("expected ':'");
    this.pos++;
    this.skip();
    return key;
  }

  private literal(word: string): void {
    if (!this.text.startsWith(word, this.pos)) this.fail("invalid literal");
    this.pos += word.length;
  }

  private scalar(): Value {
    switch (this.text[this.pos]) {
      case '"':
        return this.string();
      case "t":
        this.literal("true");
        return true;
      case "f":
        this.literal("false");
        return false;
      case "n":
        this.literal("null");
        return null;
      default:
        return this.number();
    }
  }

  private string(): string {
    this.pos++;
    let out = "";
    for (;;) {
      if (this.pos >= this.text.length) this.fail("unterminated string");
      const ch = this.text[this.pos++] as string;
      if (ch === '"') break;
      if (ch < " ") this.fail("control character in string");
      if (ch !== "\\") {
        out += ch;
        continue;
      }
      const esc = this.text[this.pos++];
      if (esc === "u") {
        const hex = this.text.slice(this.pos, this.pos + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) this.fail("invalid \\u escape");
        out += String.fromCharCode(parseInt(hex, 16));
        this.pos += 4;
      } else if (esc !== undefined && esc in ESCAPES) {
        out += ESCAPES[esc] as string;
      } else {
        this.fail("invalid escape");
      }
    }
    // Escaped or literal, a surrogate without its pair has no Unicode scalar value.
    if (!out.isWellFormed()) this.fail("lone surrogate");
    return out;
  }

  private number(): number {
    NUMBER.lastIndex = this.pos;
    const found = NUMBER.exec(this.text);
    if (found === null) this.fail("unexpected character");
    this.pos += found[0].length;
    const value = Number(found[0]);
    if (!Number.isFinite(value)) this.fail("number out of the IEEE 754 double range");
    return value;
  }
}
