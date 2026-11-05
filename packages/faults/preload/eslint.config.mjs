// ESLint for the fault preload (#14): plain ESM, Node, no TypeScript. The repo's pinned ESLint
// toolchain is installed in sdk/ts (pnpm, frozen lockfile), so @eslint/js resolves from there;
// the pre-commit hook "faults-preload-eslint" runs sdk/ts's eslint binary in this directory.
import { createRequire } from "node:module";

const require = createRequire(new URL("../../../sdk/ts/package.json", import.meta.url));
const js = require("@eslint/js");

const node = {
  console: "readonly",
  process: "readonly",
  globalThis: "readonly",
  URL: "readonly",
  TextEncoder: "readonly",
  TextDecoder: "readonly",
  Response: "readonly",
  Request: "readonly",
  Headers: "readonly",
  structuredClone: "readonly",
};

export default [
  js.configs.recommended,
  {
    files: ["**/*.mjs"],
    languageOptions: { ecmaVersion: "latest", sourceType: "module", globals: node },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      eqeqeq: ["error", "always"],
      "no-var": "error",
      "prefer-const": "error",
      "no-implicit-globals": "error",
      "no-shadow": "error",
      "no-throw-literal": "error",
      "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
];
