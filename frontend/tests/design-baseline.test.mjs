import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");

test("the Tailwind theme exposes the W1-T1 design tokens", () => {
  const globals = readFileSync(resolve(root, "app/globals.css"), "utf8");

  for (const token of [
    "--color-brand:",
    "--color-neutral-50:",
    "--color-neutral-900:",
    "--text-caption:",
    "--text-body:",
    "--text-title:",
    "--radius-sm:",
    "--radius-lg:",
    "--spacing-1:",
    "--spacing-6:",
  ]) {
    assert.match(globals, new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
});

test("the application centralizes fixed presentation for every Agent role", () => {
  const roleMapPath = resolve(root, "lib/agent-roles.ts");
  const badgePath = resolve(root, "components/agent-badge.tsx");

  assert.ok(existsSync(roleMapPath), "missing central Agent role presentation map");
  assert.ok(existsSync(badgePath), "missing reusable Agent badge component");

  const roleMap = readFileSync(roleMapPath, "utf8");
  assert.match(roleMap, /components\["schemas"\]\["AgentRole"\]/);

  for (const [role, label] of [
    ["supervisor", "Supervisor"],
    ["teaching_assistant", "助教"],
    ["learning_assistant", "助学"],
    ["evaluator", "评价"],
  ]) {
    assert.match(
      roleMap,
      new RegExp(`${role}:\\s*\\{[\\s\\S]*?label:\\s*"${label}"`),
    );
  }
});
