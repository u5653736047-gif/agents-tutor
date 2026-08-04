import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const cardPath = new URL("../components/handoff-card.tsx", import.meta.url);

async function loadHandoffCard() {
  assert.ok(existsSync(cardPath), "missing handoff card");
  return import("../components/handoff-card");
}

const pending = {
  interrupt_id: "interrupt-1",
  request: {
    plan_step_sequence: 3,
    target_agent: "teaching_assistant" as const,
    task_content: "请整理第三周的学习笔记。",
  },
};

test("the handoff card renders pending handoff details and decision buttons", async () => {
  const { HandoffCard } = await loadHandoffCard();
  assert.equal(typeof HandoffCard, "function", "missing handoff card renderer");

  const markup = renderToStaticMarkup(
    createElement(HandoffCard, {
      isDeciding: false,
      onDecide: () => {},
      pending,
    }),
  );

  // 卡片容器、标题、目标 Agent 徽标(助教)、任务内容、计划步骤
  assert.match(markup, /data-slot="handoff-card"/);
  assert.match(markup, /等待审批/);
  assert.match(markup, /助教/);
  assert.match(markup, /请整理第三周的学习笔记。/);
  assert.match(markup, /步骤 #3/);
  // 确认/拒绝按钮及 data-slot
  assert.match(markup, /data-slot="handoff-confirm"/);
  assert.match(markup, /data-slot="handoff-reject"/);
  assert.match(markup, /确认/);
  assert.match(markup, /拒绝/);
});

test("the handoff card renders nothing without a pending handoff", async () => {
  const { HandoffCard } = await loadHandoffCard();

  const markup = renderToStaticMarkup(
    createElement(HandoffCard, {
      isDeciding: false,
      onDecide: () => {},
      pending: null,
    }),
  );

  assert.equal(markup, "");
});

test("the handoff card disables both buttons while deciding", async () => {
  const { HandoffCard } = await loadHandoffCard();

  const markup = renderToStaticMarkup(
    createElement(HandoffCard, {
      isDeciding: true,
      onDecide: () => {},
      pending,
    }),
  );

  // 决策中:两个按钮都禁用,并显示处理中提示
  assert.match(markup, /data-slot="handoff-confirm"[^>]*disabled/);
  assert.match(markup, /data-slot="handoff-reject"[^>]*disabled/);
  assert.match(markup, /处理中/);
});

test("the handoff card shows the decision error message", async () => {
  const { HandoffCard } = await loadHandoffCard();

  const markup = renderToStaticMarkup(
    createElement(HandoffCard, {
      errorMessage: "会话正忙,请稍后重试。",
      isDeciding: false,
      onDecide: () => {},
      pending,
    }),
  );

  assert.match(markup, /role="alert"/);
  assert.match(markup, /会话正忙,请稍后重试。/);
});

test("the handoff card renders the modify entry while collapsed by default", async () => {
  const { HandoffCard } = await loadHandoffCard();

  const markup = renderToStaticMarkup(
    createElement(HandoffCard, {
      isDeciding: false,
      onDecide: () => {},
      pending,
    }),
  );

  // D2-T4:SSR 初始不展开,只断言「修改并继续」入口存在;编辑区展开/提交
  // 交互由组件内 useState 驱动,SSR 静态渲染无法覆盖——由 store 层透传
  // 测试(chat-store.test.ts)与代码审查保障(规格约定)。
  assert.match(markup, /data-slot="handoff-modify"/);
  assert.match(markup, /修改并继续/);
  // 初始收起:提交/取消按钮不渲染
  assert.doesNotMatch(markup, /data-slot="handoff-modify-submit"/);
  assert.doesNotMatch(markup, /data-slot="handoff-modify-cancel"/);
});
