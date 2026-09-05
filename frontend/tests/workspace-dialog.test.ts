import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const dialogPath = new URL("../components/workspace-dialog.tsx", import.meta.url);

async function loadWorkspaceDialog() {
  assert.ok(existsSync(dialogPath), "missing workspace dialog");
  return import("../components/workspace-dialog");
}

test("the workspace dialog accepts typed absolute paths and recent roots", async () => {
  const { WorkspaceDialog } = await loadWorkspaceDialog();
  const markup = renderToStaticMarkup(
    createElement(WorkspaceDialog, {
      mode: "create",
      onClose: () => undefined,
      onConfirm: async () => true,
      open: true,
      recentRoots: ["D:\\Projects\\course"],
    }),
  );

  assert.match(markup, /role="dialog"/);
  assert.match(markup, /选择工作空间/);
  assert.match(markup, /aria-label="工作空间绝对路径"/);
  assert.match(markup, /D:\\Projects\\course/);
  assert.match(markup, /浏览/);
  assert.match(markup, /使用此文件夹/);
});

test("the workspace dialog explains additional-directory authorization", async () => {
  const { WorkspaceDialog } = await loadWorkspaceDialog();
  const markup = renderToStaticMarkup(
    createElement(WorkspaceDialog, {
      mode: "add",
      onClose: () => undefined,
      onConfirm: async () => true,
      open: true,
      recentRoots: [],
    }),
  );

  assert.match(markup, /添加授权目录/);
  assert.match(markup, /当前会话/);
});
