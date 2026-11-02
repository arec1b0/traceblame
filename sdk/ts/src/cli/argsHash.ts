/**
 * aegis.tool.args_hash over JSON lines on stdin, for tests/contract/test_args_hash_parity.py.
 * Each line is {"text": string} or {"object": value}; "__NaN__", "__Infinity__" and
 * "__-Infinity__" strings in an object stand for the non-finite numbers JSON cannot carry.
 * Prints one hash per line, or "TypeError" when the value cannot be serialized.
 */
import { createInterface } from "node:readline";
import { argsHash } from "../argsHash.js";

const SPECIAL: Record<string, number> = { __NaN__: NaN, __Infinity__: Infinity, "__-Infinity__": -Infinity };

function revive(_key: string, value: unknown): unknown {
  return typeof value === "string" && value in SPECIAL ? SPECIAL[value] : value;
}

for await (const line of createInterface({ input: process.stdin })) {
  if (line.trim() === "") continue;
  const item = JSON.parse(line, revive) as { text?: string; object?: object };
  try {
    process.stdout.write(argsHash(item.text ?? (item.object as object)) + "\n");
  } catch (error) {
    if (!(error instanceof TypeError)) throw error;
    process.stdout.write("TypeError\n");
  }
}
