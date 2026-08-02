import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

test("the frontend provides the W0-T6 application skeleton", () => {
  const packageJson = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));

  for (const script of ["lint", "typecheck", "build"]) {
    assert.equal(typeof packageJson.scripts[script], "string", `missing ${script} script`);
  }
  assert.equal(typeof packageJson.dependencies.next, "string", "missing Next.js");
  assert.equal(
    typeof packageJson.devDependencies.tailwindcss,
    "string",
    "missing Tailwind CSS",
  );

  for (const relativePath of [
    "app/layout.tsx",
    "app/page.tsx",
    "app/globals.css",
    "components.json",
    "components/ui/button.tsx",
    "lib/api-base-url.ts",
    "lib/utils.ts",
    "stores/README.md",
    "postcss.config.mjs",
    "eslint.config.mjs",
    ".env.example",
  ]) {
    assert.ok(existsSync(resolve(root, relativePath)), `missing ${relativePath}`);
  }

  const page = readFileSync(resolve(root, "app/page.tsx"), "utf8");
  assert.match(page, /contracts\/api\.generated/);
  assert.match(page, /\/healthz/);

  const globals = readFileSync(resolve(root, "app/globals.css"), "utf8");
  assert.match(globals, /@import "tailwindcss"/);

  const components = JSON.parse(readFileSync(resolve(root, "components.json"), "utf8"));
  assert.equal(components.style, "new-york");
});
