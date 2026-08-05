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

// D4-T8:虚拟化与性能(长会话渲染) —————————————————————————————
test("the conversation panel fully renders short conversations without virtualization", async () => {
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: false,
      isStreaming: false,
      messages: [
        { agent: null, content: "问题一", role: "user" },
        { agent: "supervisor", content: "回答一", role: "assistant" },
        { agent: null, content: "问题二", role: "user" },
      ],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
    }),
  );

  // 短列表(<50 条)未启用虚拟化:3 条消息全部渲染,且不输出虚拟化 data-index
  assert.equal(markup.match(/data-message-role=/g)?.length, 3);
  assert.match(markup, /问题一/);
  assert.match(markup, /回答一/);
  assert.match(markup, /问题二/);
  assert.doesNotMatch(markup, /data-index/);
});

test("ConversationContent skips message rows when virtualItems is provided", async () => {
  // D4-T8 review should-fix:虚拟化启用分支的行为级测试——ConversationContent
  // 收到非 null virtualItems 时,消息行渲染由 ConversationPanel 的虚拟窗口
  // 负责(此处跳过),流式气泡/错误块/sending 等尾部块仍渲染。
  const { ConversationContent } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(ConversationContent, {
      isSending: true,
      isStreaming: false,
      messages: [
        { agent: null, content: "不应渲染的全量消息", role: "user" },
        { agent: "supervisor", content: "全量路径才有的回答", role: "assistant" },
      ],
      runError: null,
      streamingAgent: null,
      streamingMessage: null,
      virtualItems: [{ index: 0 }],
    }),
  );

  // 消息行被跳过(虚拟窗口负责),但尾部块仍在
  assert.doesNotMatch(markup, /不应渲染的全量消息/);
  assert.doesNotMatch(markup, /全量路径才有的回答/);
  assert.doesNotMatch(markup, /data-message-role=/);
  assert.match(markup, /正在生成回答/);
});

test("the conversation panel virtualizes long message lists behind a threshold", () => {
  const panel = readFileSync(panelPath, "utf8");

  // 阈值开关:超过 50 条才启用虚拟化(短会话保持既有全量渲染)
  assert.match(panel, /useVirtualizer/);
  assert.match(panel, /enabled: messages\.length > 50/);
  // 动态行高测量 + 滚动容器贴底跟随判定(防回归)
  assert.match(panel, /measureElement/);
  assert.match(panel, /onScroll=\{handleScroll\}/);
  assert.match(panel, /isNearBottom\(/);
  assert.match(panel, /data-slot="message-list"/);
});

test("MessageRow renders a single message row for the virtualized window", async () => {
  const { MessageRow } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(MessageRow, {
      message: {
        agent: "supervisor",
        content: "窗口内的回答",
        role: "assistant",
      },
      index: 42,
    }),
  );

  assert.match(markup, /data-message-role="assistant"/);
  assert.match(markup, /窗口内的回答/);
  assert.match(markup, /Supervisor/);
});

test("MessageRow renders data-index on virtualized rows for measureElement", async () => {
  const { MessageRow } = await loadConversationPanel();

  const markup = renderToStaticMarkup(
    createElement(MessageRow, {
      message: { agent: null, content: "窗口中的问题", role: "user" },
      index: 42,
      dataIndex: 42,
      measureRef: () => undefined,
    }),
  );

  // measureElement 依赖 data-index 定位行索引;行内容与全量路径一致
  assert.match(markup, /data-index="42"/);
  assert.match(markup, /data-message-role="user"/);
  assert.match(markup, /窗口中的问题/);
});
