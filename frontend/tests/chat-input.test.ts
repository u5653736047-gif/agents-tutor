import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const chatInputPath = new URL("../components/chat-input.tsx", import.meta.url);

async function loadChatInput() {
  assert.ok(existsSync(chatInputPath), "missing chat input component");
  return import("../components/chat-input");
}

test("the chat input renders a multiline form and disables controls while sending", async () => {
  const { ChatInputContent } = await loadChatInput();

  assert.equal(typeof ChatInputContent, "function", "missing chat input content renderer");
  const idleMarkup = renderToStaticMarkup(
    createElement(ChatInputContent, {
      isSending: false,
      onChange: () => {},
      onSubmit: () => {},
      value: "请帮我解释这个概念",
    }),
  );
  const sendingMarkup = renderToStaticMarkup(
    createElement(ChatInputContent, {
      isSending: true,
      onChange: () => {},
      onSubmit: () => {},
      value: "请帮我解释这个概念",
    }),
  );

  assert.match(idleMarkup, /<form/);
  assert.match(idleMarkup, /data-slot="chat-input"/);
  assert.match(idleMarkup, /<textarea/);
  assert.match(idleMarkup, /rows="3"/);
  assert.doesNotMatch(idleMarkup, /<textarea[^>]* disabled=""/);
  assert.match(sendingMarkup, /<textarea[^>]* disabled=""/);
  assert.match(sendingMarkup, /<button[^>]* disabled=""/);
});

test("the chat input reserves plain Enter for send and leaves Shift+Enter as a newline", async () => {
  const { isSendShortcut, normalizeMessage } = await loadChatInput();

  assert.equal(isSendShortcut({ isComposing: false, key: "Enter", shiftKey: false }), true);
  assert.equal(isSendShortcut({ isComposing: false, key: "Enter", shiftKey: true }), false);
  assert.equal(isSendShortcut({ isComposing: true, key: "Enter", shiftKey: false }), false);
  assert.equal(isSendShortcut({ isComposing: false, key: "a", shiftKey: false }), false);
  assert.equal(normalizeMessage("  \n  "), null);
  assert.equal(normalizeMessage("  保留输入内容  "), "保留输入内容");
});

// ── D4-T3 输入区增强:自适应高度 + 停止生成 ─────────────────────

test("clampTextareaHeight clamps the textarea height to the min baseline and max cap", async () => {
  const { clampTextareaHeight } = await loadChatInput();

  assert.equal(typeof clampTextareaHeight, "function", "missing height clamp helper");
  // 低于基线(空输入/短内容)夹到 96px 基线
  assert.equal(clampTextareaHeight(60, 192), 96);
  assert.equal(clampTextareaHeight(96, 192), 96);
  // 中间值原样返回
  assert.equal(clampTextareaHeight(100, 192), 100);
  // 超过上限(8 行 × 24px = 192px)夹到 192px
  assert.equal(clampTextareaHeight(500, 192), 192);
});

test("the chat input swaps the send button for a stop button while streaming", async () => {
  const { ChatInputContent } = await loadChatInput();

  const idleMarkup = renderToStaticMarkup(
    createElement(ChatInputContent, {
      isSending: false,
      onChange: () => {},
      onSubmit: () => {},
      value: "等待输入",
    }),
  );
  const streamingMarkup = renderToStaticMarkup(
    createElement(ChatInputContent, {
      isSending: true,
      isStreaming: true,
      onChange: () => {},
      onStop: () => {},
      onSubmit: () => {},
      value: "正在生成回答",
    }),
  );

  // 流式时:停止按钮(data-slot + 文案)替代发送按钮
  assert.match(streamingMarkup, /data-slot="stop-generating"/);
  assert.match(streamingMarkup, /停止生成/);
  assert.doesNotMatch(streamingMarkup, />发送</);
  // 非流式:不渲染停止按钮,发送按钮保持(isStreaming 可选,向后兼容)
  assert.doesNotMatch(idleMarkup, /data-slot="stop-generating"/);
  assert.match(idleMarkup, />发送</);
});

test("ChatInput renders without a stop button in the initial SSR state", async () => {
  // D4-T3:SSR 用 store 初始 state 快照(isStreaming=false),停止按钮
  // 的「真触发」由 chat-store-stream.test.ts 的 cancelStreaming 用例 +
  // 手动验收覆盖(zustand v5 的 useStore 在 renderToStaticMarkup 下
  // setState 不生效,D1-T2 已知)。
  const { ChatInput } = await loadChatInput();

  const markup = renderToStaticMarkup(createElement(ChatInput));

  assert.match(markup, /data-slot="chat-input"/);
  assert.doesNotMatch(markup, /data-slot="stop-generating"/);
  assert.match(markup, />发送</);
});
// 注:ChatInput 的 isSending={isSending || isStreaming} 组合逻辑没有
// 渲染级测试——zustand v5 的 useStore 在 renderToStaticMarkup(SSR)下
// 用初始 state 快照,setState 不会反映到服务端渲染输出,该断言方式
// 不可行。组合逻辑由 chat-store-stream.test.ts 的 isStreaming 状态
// 生命周期用例 + chat-input.test.ts 的 ChatInputContent disabled 用例
// 间接覆盖(两者合并即该一行表达式的两端)。

// ── D4-T4 快捷指令候选列表 ─────────────────────────────

test("the chat input does not render the slash command list in the initial SSR state", async () => {
  // D4-T4:候选列表由键盘/鼠标交互触发(showList 初始为 false),与
  // D4-T3 停止按钮同理,renderToStaticMarkup 下无法触发交互,SSR
  // 初始态恒不渲染;键盘导航/选中/Escape 关闭由手动验收覆盖。
  const { ChatInputContent } = await loadChatInput();

  const markup = renderToStaticMarkup(
    createElement(ChatInputContent, {
      isSending: false,
      onChange: () => {},
      onSubmit: () => {},
      value: "/",
    }),
  );

  assert.doesNotMatch(markup, /data-slot="slash-commands"/);
  assert.doesNotMatch(markup, /data-slot="slash-command-item"/);
});
