import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const appShellPath = new URL("../components/app-shell.tsx", import.meta.url);

async function loadAppShell() {
  assert.ok(existsSync(appShellPath), "missing two-column application shell");
  return import("../components/app-shell");
}

test("the application shell renders a desktop session sidebar and empty conversation area", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  assert.match(markup, /data-layout="desktop-two-column"/);
  assert.match(markup, /data-slot="session-sidebar"/);
  assert.match(markup, /新建会话/);
  assert.match(markup, /暂无会话/);
  assert.match(markup, /data-slot="conversation-area"/);
  assert.match(markup, /后端已连接/);
});

test("selecting a session asks the store to load its history", () => {
  const sidebarPath = new URL("../components/session-sidebar.tsx", import.meta.url);

  assert.ok(existsSync(sidebarPath), "missing session sidebar");
  const sidebar = readFileSync(sidebarPath, "utf8");
  assert.match(sidebar, /loadCurrentSessionMessages/);
  assert.match(sidebar, /selectSession\(session\.session_id\);\s*void loadCurrentSessionMessages\(\)/);
});
