// assistant-ui 接入(T9):审批卡片挂接的测试。
//
// 测试策略说明(与项目约定对齐):zustand v5 的 useSyncExternalStore 服务端
// 快照读 getInitialState()——renderToStaticMarkup 期间 setState 注入不可见,
// 因此 store 连接组件的行为状态不在 SSR 下断言(先例:conversation-panel.test.ts
// 只渲染 props 驱动的 ConversationContent;app-shell.test.ts 用 SSR 初始态 +
// 源码正则)。本文件分两层:
//   1. SSR 初始态:无 pending 时两张卡片零渲染(初始状态即无 pending);
//   2. 源码正则:挂接 wiring——store 订阅字段、props 映射、错误码映射、
//      在 Thread 中的挂载位置(Messages 之后)。
// 卡片自身行为(决策状态机/modify 表单/焦点管理)由既有
// handoff-card.test.ts 与 terminal-approval-card.test.ts 覆盖(未改动组件);
// decide action 的行为由 chat-store.test.ts 覆盖。
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const approvalCardsPath = new URL(
  "../components/assistant-ui/approval-cards.tsx",
  import.meta.url,
);
const threadPath = new URL(
  "../components/assistant-ui/assistant-thread.tsx",
  import.meta.url,
);

async function loadApprovalCards() {
  assert.ok(existsSync(approvalCardsPath), "missing approval cards");
  return import("../components/assistant-ui/approval-cards");
}

test("approval cards render nothing in the initial store state", async () => {
  const { ApprovalCards } = await loadApprovalCards();

  // 模块级 store 的初始状态即无 pending(SSR 服务端快照 = 初始状态)
  const markup = renderToStaticMarkup(createElement(ApprovalCards));
  assert.doesNotMatch(markup, /data-slot="handoff-card"/);
  assert.doesNotMatch(markup, /data-slot="terminal-approval-card"/);
});

test("approval cards subscribe the exact store fields and props mapping", () => {
  const source = readFileSync(approvalCardsPath, "utf8");

  // store 订阅:双通道 pending + 决策中标记 + 决策 action + 错误映射源
  for (const field of [
    "pendingHandoff",
    "pendingToolApproval",
    "isDecidingHandoff",
    "isDecidingToolApproval",
    "decideHandoff",
    "decideToolApproval",
    "requestError",
  ]) {
    assert.match(source, new RegExp(`state\\.${field}`), `missing ${field}`);
  }
  // props 映射与旧面板逐项一致(conversation-panel.tsx L675-695)
  assert.match(source, /onDecide=\{\(action, modifications\) =>/);
  assert.match(source, /decideHandoff\(action, modifications\)/);
  assert.match(source, /decideToolApproval\(action\)/);
  // 错误码映射:session_busy 双卡共享,tool_approval_not_pending 仅终端卡
  assert.match(source, /requestError\?\.code === "session_busy"/);
  assert.match(source, /requestError\?\.code === "tool_approval_not_pending"/);
});

test("the thread mounts approval cards after the message list", () => {
  const source = readFileSync(threadPath, "utf8");

  // 挂载位置语义:Messages 之后、输入区之前(与旧路径的消息流尾部一致)
  const messagesAt = source.indexOf("ThreadPrimitive.Messages");
  const cardsAt = source.indexOf("<ApprovalCards");
  const inputAt = source.indexOf('data-slot="chat-input-area"');
  assert.ok(messagesAt > 0 && cardsAt > messagesAt, "cards after messages");
  assert.ok(inputAt > cardsAt, "cards before input area");
});
