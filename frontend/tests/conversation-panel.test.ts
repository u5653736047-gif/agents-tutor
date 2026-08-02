import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const panelPath = new URL("../components/conversation-panel.tsx", import.meta.url);

async function loadConversationPanel() {
  assert.ok(existsSync(panelPath), "missing conversation panel");
  return import("../components/conversation-panel");
}

test("the conversation panel distinguishes messages and shows Agent, error, and sending state", async () => {
  const { ConversationContent } = await loadConversationPanel();

  assert.equal(typeof ConversationContent, "function", "missing conversation content renderer");
  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: true,
      messages: [
        { agent: null, content: "用户的问题", role: "user" },
        { agent: "supervisor", content: "助手的回答", role: "assistant" },
      ],
      runError: {
        agent: "supervisor",
        error_code: "model_call_failed",
        message: "模型暂时不可用。",
      },
    }),
  );

  assert.match(markup, /data-message-role="user"/);
  assert.match(markup, /data-message-role="assistant"/);
  assert.match(markup, /用户的问题/);
  assert.match(markup, /助手的回答/);
  assert.match(markup, /Supervisor/);
  assert.match(markup, /正在生成回答/);
  assert.match(markup, /模型暂时不可用。/);
});

test("the conversation panel keeps an end anchor for automatic scrolling", () => {
  assert.ok(existsSync(panelPath), "missing conversation panel");
  const panel = readFileSync(panelPath, "utf8");

  assert.match(panel, /scrollIntoView/);
  assert.match(panel, /data-slot="conversation-end"/);
});
