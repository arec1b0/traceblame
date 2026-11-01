import { defineConfig } from "vitest/config";

const MIN = Number(process.env["COV_MIN"] ?? "80");

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      exclude: ["src/cli/**"],
      // The shim's coverage gate: COV_MIN from the Makefile (CLAUDE.md), 80 when run directly.
      thresholds: { lines: MIN, branches: MIN, functions: MIN, statements: MIN },
      reporter: ["text-summary", "text"],
    },
  },
});
