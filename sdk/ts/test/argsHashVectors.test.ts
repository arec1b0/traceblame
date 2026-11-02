// The TypeScript runner over the profile's golden vectors (schemas/profile/vectors/README.md):
// every vector of every file in v1/, and the file's algorithm_version must be ours.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ALGORITHM_VERSION, argsHash } from "../src/argsHash.js";
import { NotIJSONError, canonicalize } from "../src/jcs.js";

interface Vector {
  id: string;
  input: string;
  canonical: string | null;
  expected: string;
}
interface VectorFile {
  attribute: string;
  algorithm_version: number;
  vectors: Vector[];
}

const DIR = join(dirname(fileURLToPath(import.meta.url)), "../../../schemas/profile/vectors/v1");
const FILES = readdirSync(DIR).filter((f) => f.endsWith(".json"));

it("the registry has files", () => {
  expect(FILES.length).toBeGreaterThan(0);
});

describe.each(FILES)("%s", (file) => {
  const data = JSON.parse(readFileSync(join(DIR, file), "utf8")) as VectorFile;

  it("is the args_hash registry at our algorithm_version", () => {
    // A new attribute with vectors needs its own implementation here before it can pass.
    expect(data.attribute).toBe("aegis.tool.args_hash");
    expect(data.algorithm_version).toBe(ALGORITHM_VERSION);
    expect(data.vectors.length).toBeGreaterThan(0);
  });

  it.each(data.vectors.map((v) => [v.id, v] as const))("%s", (_id, v) => {
    if (v.canonical === null) {
      expect(() => canonicalize(v.input)).toThrow(NotIJSONError);
    } else {
      expect(canonicalize(v.input)).toBe(v.canonical);
    }
    expect(argsHash(v.input)).toBe(v.expected);
  });
});
