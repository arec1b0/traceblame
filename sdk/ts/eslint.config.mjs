import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/", "coverage/", "node_modules/"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: { allowDefaultProject: ["eslint.config.mjs"] } },
    },
  },
  {
    files: ["eslint.config.mjs"],
    ...tseslint.configs.disableTypeChecked,
  },
);
