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
// 注:ChatInput 的 isSending={isSending || isStreaming} 组合逻辑没有
// 渲染级测试——zustand v5 的 useStore 在 renderToStaticMarkup(SSR)下
// 用初始 state 快照,setState 不会反映到服务端渲染输出,该断言方式
// 不可行。组合逻辑由 chat-store-stream.test.ts 的 isStreaming 状态
// 生命周期用例 + chat-input.test.ts 的 ChatInputContent disabled 用例
// 间接覆盖(两者合并即该一行表达式的两端)。
