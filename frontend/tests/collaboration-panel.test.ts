// D2-T2:协作过程面板的纯组件测试。
// 用 renderToStaticMarkup 直接渲染组件(传 props,不碰 zustand store,
// 与 conversation-panel.test.ts 的先例一致)。折叠是客户端交互
// (useState + onClick),SSR 无法模拟点击,因此只测「初始默认展开」。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const panelPath = new URL("../components/collaboration-panel.tsx", import.meta.url);

async function loadCollaborationPanel() {
  assert.ok(existsSync(panelPath), "missing collaboration panel");
  return import("../components/collaboration-panel");
}

// 全空 props:既无计划也无事件,应渲染空态
const emptyProps = {
  currentAgent: null,
  events: [],
  taskPlan: null,
  taskResults: null,
};

test("the collaboration panel shows a placeholder when there is no plan or events", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();

  const markup = renderToStaticMarkup(createElement(CollaborationPanel, emptyProps));

  assert.match(markup, /data-slot="collaboration-panel"/);
  assert.match(markup, /data-slot="collaboration-empty"/);
  assert.match(markup, /暂无协作过程/);
  // 空态时不应渲染计划条与时间线
  assert.doesNotMatch(markup, /data-slot="plan-steps"/);
  assert.doesNotMatch(markup, /data-slot="event-timeline"/);
});

test("the collaboration panel renders events in sequence order with expandable tool details", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();

  // 故意乱序传入:验证按 sequence 升序渲染
  const events = [
    { event_type: "tool_result", sequence: 4, tool_name: "search_notes", success: true },
    { event_type: "thinking", sequence: 1, agent: "learning_assistant", content: "正在思考如何组织回答" },
    { event_type: "agent_switch", sequence: 5, agent: "supervisor" },
    { event_type: "tool_call", sequence: 3, tool_name: "search_notes", plan_step_sequence: 1 },
    { event_type: "done", sequence: 6 },
  ];

  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, { ...emptyProps, events }),
  );

  // 按 sequence 顺序:thinking(1) → tool_call(3) → tool_result(4) → agent_switch(5)
  const thinkingAt = markup.indexOf("正在思考如何组织回答");
  const toolCallAt = markup.indexOf("search_notes");
  const toolResultAt = markup.lastIndexOf("search_notes");
  const switchAt = markup.indexOf("→");
  assert.ok(thinkingAt >= 0 && toolCallAt >= 0 && toolResultAt >= 0 && switchAt >= 0);
  assert.ok(thinkingAt < toolCallAt, "thinking 应排在 tool_call 前");
  assert.ok(toolCallAt < toolResultAt, "tool_call 应排在 tool_result 前");
  assert.ok(toolResultAt < switchAt, "tool_result 应排在 agent_switch 前");

  // 工具行摘要保持紧凑，详情作为原生 details 内容随 DOM 提供。
  assert.match(markup, /data-slot="tool-row"/);
  assert.match(markup, /data-slot="tool-details"/);
  assert.match(markup, /search_notes/);
  assert.doesNotMatch(markup, /参数/);
  assert.doesNotMatch(markup, /结果正文/);
  assert.match(markup, /所属计划步骤:1/);
  assert.doesNotMatch(markup, /耗时/);

  // agent_switch 提示行出现,终态事件(done)被忽略
  assert.match(markup, /→/);
  assert.match(markup, /Supervisor/);
  assert.doesNotMatch(markup, /done/);
});

test("the collaboration panel gives known agent tools readable activity labels", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();
  const events = [
    {
      agent: "supervisor",
      event_type: "tool_call",
      sequence: 1,
      tool_name: "ask_learning_assistant",
    },
    {
      agent: "learning_assistant",
      event_type: "tool_call",
      sequence: 2,
      tool_name: "search_knowledge",
    },
  ];

  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, { ...emptyProps, events }),
  );

  assert.match(markup, /调用助学助手/);
  assert.match(markup, /检索课程知识库/);
  assert.doesNotMatch(markup, /ask_learning_assistant/);
});

test("the collaboration panel exposes model reasoning and detailed tool activity", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();
  const events = [
    {
      agent: "learning_assistant",
      content: "先区分前向传播与反向传播，再解释链式法则。",
      event_type: "reasoning",
      message_id: "reasoning-step-1",
      sequence: 2,
    },
    {
      agent: "learning_assistant",
      event_type: "tool_call",
      input_summary: '{"query":"反向传播","api_key":"[REDACTED]"}',
      sequence: 3,
      tool_call_id: "call-search-1",
      tool_name: "search_knowledge",
    },
    {
      agent: "learning_assistant",
      event_type: "tool_result",
      output_summary: '{"found":true,"hits":2}',
      sequence: 4,
      success: true,
      tool_call_id: "call-search-1",
      tool_name: "search_knowledge",
    },
    {
      agent: "evaluator",
      content: "核对讲解是否覆盖梯度方向。",
      event_type: "reasoning",
      message_id: "reasoning-child-1",
      parent_tool_call_id: "call-search-1",
      sequence: 5,
    },
  ];

  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, { ...emptyProps, events }),
  );

  assert.match(markup, /data-slot="reasoning-block"/);
  assert.match(markup, /模型思考/);
  assert.match(markup, /先区分前向传播与反向传播/);
  assert.match(markup, /data-slot="tool-details"/);
  assert.match(markup, /工具输入/);
  assert.match(markup, /反向传播/);
  assert.match(markup, /工具输出/);
  assert.match(markup, /&quot;hits&quot;:2/);
  assert.match(markup, /data-parent-tool-call-id="call-search-1"/);
  assert.match(markup, /核对讲解是否覆盖梯度方向/);
  assert.doesNotMatch(markup, /sk-never-show/);
});

test("the collaboration panel renders coalesced subagent output", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();
  const events = [
    {
      agent: "learning_assistant",
      content: "子代理正在整理知识点",
      event_type: "message_delta",
      message_id: "worker-answer",
      sequence: 2,
    },
  ];

  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, { ...emptyProps, events }),
  );

  assert.match(markup, /data-slot="subagent-message"/);
  assert.match(markup, /助学/);
  assert.match(markup, /子代理正在整理知识点/);
});

test("the collaboration panel shows plan steps with current highlight and result marks", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();

  const taskPlan = {
    current_step_index: 1,
    status: "active",
    steps: [
      { sequence: 1, description: "检索相关笔记", target_agent: "learning_assistant" },
      { sequence: 2, description: "评估回答质量", target_agent: "evaluator" },
    ],
  };
  const taskResults = [
    { step_sequence: 1, success: true, target_agent: "learning_assistant", output: "找到 3 条笔记" },
    { step_sequence: 2, success: false, target_agent: "evaluator", error_code: "tool_call_failed", output: null },
  ];

  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, {
      currentAgent: null,
      events: [],
      taskPlan,
      taskResults,
    }),
  );

  assert.match(markup, /data-slot="plan-steps"/);
  assert.match(markup, /检索相关笔记/);
  assert.match(markup, /评估回答质量/);
  assert.match(markup, /进行中/);
  // 第 2 步(current_step_index=1)高亮
  assert.match(markup, /data-current="true"/);
  // 第 1 步成功打勾,第 2 步失败打叉并显示 error_code
  assert.match(markup, /data-result="success"/);
  assert.match(markup, /data-result="failed"/);
  assert.match(markup, /tool_call_failed/);
});

test("the collaboration panel highlights events of the active agent", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();

  const events = [
    { event_type: "thinking", sequence: 1, agent: "learning_assistant", content: "整理笔记" },
    { event_type: "thinking", sequence: 2, agent: "supervisor", content: "规划任务" },
  ];

  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, {
      ...emptyProps,
      currentAgent: "learning_assistant",
      events,
    }),
  );

  // 只有 learning_assistant 的那条事件行带 data-active="true"
  assert.equal((markup.match(/data-active="true"/g) ?? []).length, 1);
  const activeAt = markup.indexOf("data-active=\"true\"");
  assert.ok(activeAt >= 0 && activeAt < markup.indexOf("整理笔记"));
  // 另一条(supervisor)不高亮
  assert.ok(markup.indexOf("data-active=\"true\"") < markup.indexOf("规划任务"));
});

test("the collaboration panel is expanded by default and exposes a toggle", async () => {
  const { CollaborationPanel } = await loadCollaborationPanel();

  const events = [
    { event_type: "thinking", sequence: 1, agent: "supervisor", content: "规划中" },
  ];

  // 折叠/展开是客户端交互(useState + onClick),SSR 无法模拟点击;
  // 可测点是「初始状态默认展开」:时间线与切换按钮都出现在 markup 中。
  const markup = renderToStaticMarkup(
    createElement(CollaborationPanel, { ...emptyProps, events }),
  );

  assert.match(markup, /data-slot="collaboration-toggle"/);
  assert.match(markup, /aria-expanded="true"/);
  assert.match(markup, /data-slot="event-timeline"/);
  assert.match(markup, /规划中/);
});
