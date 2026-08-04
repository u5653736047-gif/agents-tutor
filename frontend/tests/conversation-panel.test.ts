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

// D2-T5:错误降级 UX——runError 分类渲染与重试按钮 ———————————————————
test("the conversation panel renders a categorized run error with a retry button when onRetry is provided", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      onRetry: () => undefined,
      runError: {
        agent: "supervisor",
        error_code: "session_busy",
        message: "A request is already running.",
      },
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 分类标题 + 说明 + 原始 message 保留 + 重试按钮
  assert.match(markup, /会话正忙/);
  assert.match(markup, /该会话正在处理其他请求/);
  assert.match(markup, /A request is already running/);
  assert.match(markup, /data-slot="run-error-retry"/);
  // 按钮文案优先用预设 action(「稍后再试」)
  assert.match(markup, /稍后再试/);
});

test("the conversation panel omits the retry button when onRetry is absent", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      runError: {
        agent: "supervisor",
        error_code: "model_call_failed",
        message: "模型暂时不可用。",
      },
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 分类文案(标题 + 说明)渲染;无 onRetry 时无重试按钮
  assert.match(markup, /模型服务暂不可用/);
  assert.match(markup, /模型调用失败/);
  assert.doesNotMatch(markup, /data-slot="run-error-retry"/);
});

test("the conversation panel shows a network error block with retry for null request code", async () => {
  const { ConversationContent } = await loadConversationPanel();
  const { ApiClientError } = await import("../lib/api-client");

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      onRetry: () => undefined,
      requestError: new ApiClientError("网络失败。", { code: null, status: null }),
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 网络失败(code===null)在消息流内显示错误块 + 重试按钮
  assert.match(markup, /网络请求失败/);
  assert.match(markup, /请检查网络连接后重试/);
  assert.match(markup, /data-slot="request-error-network"/);
  assert.match(markup, /data-slot="request-error-retry"/);
  // 非网络错误码不渲染该块(如 session_busy 只走侧栏映射)
  const busyMarkup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [],
      requestError: new ApiClientError("忙。", { code: "session_busy", status: null }),
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );
  assert.doesNotMatch(busyMarkup, /data-slot="request-error-network"/);
});
