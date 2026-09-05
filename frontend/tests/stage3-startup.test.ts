import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const scriptPath = new URL("../../scripts/start-stage3.ps1", import.meta.url);
const readmePath = new URL("../../README.md", import.meta.url);

test("the root startup script launches both local services with the required runtime setup", () => {
  assert.ok(existsSync(scriptPath), "missing root startup script");
  const script = readFileSync(scriptPath, "utf8");

  assert.match(script, /Start-Process/);
  assert.match(script, /-WindowStyle Hidden/);
  assert.match(script, /"-m", "uvicorn"/);
  assert.match(script, /"--reload"/);
  assert.match(script, /node\.exe/);
  assert.match(script, /next\\\\dist\\\\bin\\\\next/);
  assert.match(script, /PYTHONPATH/);
  assert.match(script, /DEEPSEEK_API_KEY/);
  assert.match(script, /API_SESSION_STORE_PATH/);
  assert.match(script, /API_CHECKPOINT_PATH/);
});

test("the root startup script stops the uvicorn reload process tree", () => {
  const script = readFileSync(scriptPath, "utf8");

  assert.match(script, /taskkill\.exe/);
  assert.match(script, /\/T/);
});

test("the README documents the one-command startup, model environment, and demo user", () => {
  const readme = readFileSync(readmePath, "utf8");

  assert.match(readme, /scripts[\\/]start-stage3\.ps1/);
  assert.match(readme, /DEEPSEEK_MODEL/);
  assert.match(readme, /DEEPSEEK_BASE_URL/);
  assert.match(readme, /DEEPSEEK_API_KEY/);
  assert.match(readme, /NEXT_PUBLIC_API_BASE_URL/);
  assert.match(readme, /demo-user/);
});
