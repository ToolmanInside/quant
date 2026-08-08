import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Python/tooling directories that eslint must not traverse:
    ".pytest_cache/**",
    ".venv/**",
    ".quant-state/**",
    ".npm-cache/**",
    ".pip-cache/**",
    ".pnpm-store/**",
    "backend/**",
    "tests_py/**",
    "scripts/**",
    "logs/**",
    "outputs/**",
    "dist/**",
  ]),
]);

export default eslintConfig;
