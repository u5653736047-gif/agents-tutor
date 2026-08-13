// assistant-ui 接入(T5-T8):part 渲染器的 SSR 组件测试。
// 与 collaboration-panel.test.ts 同一先例:renderToStaticMarkup 直渲组件,
// data-slot 锚点断言;折叠交互是客户端行为,SSR 只测初始 open 态。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const reasoningPartPath = new URL(
  "../components/assistant-ui/parts/reasoning-part.tsx",
  import.meta.url,
);
const toolCallPartPath = new URL(
  "../components/assistant-ui/parts/tool-call-part.tsx",
  import.meta.url,
);
const agentSwitchPartPath = new URL(
  "../components/assistant-ui/parts/agent-switch-part.tsx",
  import.meta.url,
);
const subagentOutputPartPath = new URL(
  "../components/assistant-ui/parts/subagent-output-part.tsx",
  import.meta.url,
);
const footerPath = new URL(
  "../components/assistant-ui/assistant-message-footer.tsx",
  import.meta.url,
);

async function loadReasoningPart() {
  assert.ok(existsSync(reasoningPartPath), "missing reasoning part");
  return import("../components/assistant-ui/parts/reasoning-part");
}

async function loadToolCallPart() {
  assert.ok(existsSync(toolCallPartPath), "missing tool-call part");
  return import("../components/assistant-ui/parts/tool-call-part");
}

async function loadAgentSwitchPart() {
  assert.ok(existsSync(agentSwitchPartPath), "missing agent-switch part");
  return import("../components/assistant-ui/parts/agent-switch-part");
}

async function loadSubagentOutputPart() {
  assert.ok(existsSync(subagentOutputPartPath), "missing subagent-output part");
  return import("../components/assistant-ui/parts/subagent-output-part");
}

async function loadFooter() {
  assert.ok(existsSync(footerPath), "missing assistant message footer");
  return import("../components/assistant-ui/assistant-message-footer");
}

// —— ReasoningPart(T5) ——

test("reasoning part streams open and shows the producer agent badge", async () => {
  const { ReasoningPart } = await loadReasoningPart();

  const markup = renderToStaticMarkup(
    createElement(ReasoningPart, {
      type: "reasoning",
      text: "正在分析学习需求",
      status: { type: "running" },
      providerMetadata: { "agents-tutor": { agent: "teaching_assistant" } },
    }),
  );

  assert.match(markup, /data-slot="reasoning-block"/);
  // 流式中自动展开
  assert.match(markup, /<details[^>]*open/);
  // 产出角色徽章(助教)与思维链标题
  assert.match(markup, /助教/);
  assert.match(markup, /思维链/);
  assert.match(markup, /正在分析学习需求/);
});

test("reasoning part collapses when complete and tolerates dirty metadata", async () => {
  const { ReasoningPart } = await loadReasoningPart();

  const settled = renderToStaticMarkup(
    createElement(ReasoningPart, {
      type: "reasoning",
      text: "已完成的思考",
      status: { type: "complete" },
    }),
  );
  // 结束后默认折叠(open 属性缺席),无角色时不渲染徽章
  assert.doesNotMatch(settled, /<details[^>]*open/);
  assert.doesNotMatch(settled, /data-slot="agent-badge"/);
  assert.match(settled, /已完成的思考/);

  // 脏数据(非法 providerMetadata 结构)不崩溃、不渲染徽章
  const dirty = renderToStaticMarkup(
    createElement(ReasoningPart, {
      type: "reasoning",
      text: "x",
      status: { type: "complete" },
      providerMetadata: { "agents-tutor": { agent: "not-a-role" } },
    }),
  );
  assert.doesNotMatch(dirty, /data-slot="agent-badge"/);
});

// —— ToolCallPart(T4 基础/T6 锚点) ——

test("tool call part renders pending, success and error states", async () => {
  const { ToolCallPart } = await loadToolCallPart();

  const pending = renderToStaticMarkup(
    createElement(ToolCallPart, {
      type: "tool-call",
      toolCallId: "tc-1",
      toolName: "search_knowledge",
      args: {},
      argsText: "query=反向传播",
      status: { type: "running" },
    }),
  );
  assert.match(pending, /data-slot="tool-row"/);
  assert.match(pending, /执行中/);
  // 执行中只有参数,没有结果区
  assert.match(pending, /data-slot="tool-details"/);
  assert.doesNotMatch(pending, /data-slot="tool-result"/);

  const succeeded = renderToStaticMarkup(
    createElement(ToolCallPart, {
      type: "tool-call",
      toolCallId: "tc-1",
      toolName: "search_knowledge",
      args: {},
      argsText: "query=反向传播",
      result: "命中 3 条",
      isError: false,
      status: { type: "complete" },
    }),
  );
  assert.match(succeeded, /完成/);
  assert.match(succeeded, /命中 3 条/);

  const failed = renderToStaticMarkup(
    createElement(ToolCallPart, {
      type: "tool-call",
      toolCallId: "tc-2",
      toolName: "shell",
      args: {},
      argsText: "",
      result: "超时",
      isError: true,
      status: { type: "complete" },
    }),
  );
  assert.match(failed, /失败/);
  assert.match(failed, /超时/);
});

test("tool call part shows the Chinese activity label for known tools", async () => {
  const { ToolCallPart } = await loadToolCallPart();

  const markup = renderToStaticMarkup(
    createElement(ToolCallPart, {
      type: "tool-call",
      toolCallId: "tc-9",
      toolName: "search_knowledge",
      args: {},
      argsText: "query=x",
      status: { type: "running" },
    }),
  );
  assert.match(markup, /检索课程知识库/);

  // 未登记工具诚实降级为原始 tool_name
  const unknown = renderToStaticMarkup(
    createElement(ToolCallPart, {
      type: "tool-call",
      toolCallId: "tc-10",
      toolName: "future_tool",
      args: {},
      argsText: "",
      status: { type: "running" },
    }),
  );
  assert.match(unknown, /future_tool/);
});

// T6 受控复制守卫:新映射表必须与旧面板(collaboration-panel.tsx)保持同步——
// 旧表每个「工具名: 中文名」条目都要在新表源码中原样出现,防止两处漂移。
test("tool activity labels stay in sync with the legacy panel table", async () => {
  const { readFileSync } = await import("node:fs");
  const labelsPath = new URL("../lib/tool-activity-labels.ts", import.meta.url);
  const legacyPath = new URL(
    "../components/collaboration-panel.tsx",
    import.meta.url,
  );
  assert.ok(existsSync(labelsPath), "missing tool-activity-labels lib");
  const labelsSource = readFileSync(labelsPath, "utf8");
  const legacySource = readFileSync(legacyPath, "utf8");

  const entries = legacySource.match(/\w+: "[^"]+",/g) ?? [];
  const toolEntries = entries.filter((entry) =>
    /^(ask_|create_|detect_|search_|submit_|shell)/.test(entry),
  );
  assert.ok(toolEntries.length >= 8, "legacy label table not found");
  for (const entry of toolEntries) {
    assert.ok(
      labelsSource.includes(entry.replace(/,$/, "")),
      `label entry missing in tool-activity-labels.ts: ${entry}`,
    );
  }
});

// —— AgentSwitchPart / SubagentOutputPart(T4 基础/T8 锚点) ——

test("agent switch part renders a divider with the target role badge", async () => {
  const { AgentSwitchPart } = await loadAgentSwitchPart();

  const markup = renderToStaticMarkup(
    createElement(AgentSwitchPart, {
      type: "data",
      name: "agent-switch",
      data: { agent: "learning_assistant" },
      status: { type: "complete" },
    }),
  );
  assert.match(markup, /data-slot="agent-switch"/);
  assert.match(markup, /助学/);

  // 非法数据零渲染(宽容读取)
  const dirty = renderToStaticMarkup(
    createElement(AgentSwitchPart, {
      type: "data",
      name: "agent-switch",
      data: {},
      status: { type: "complete" },
    }),
  );
  assert.equal(dirty, "");
});

test("subagent output part renders the worker card with role badge", async () => {
  const { SubagentOutputPart } = await loadSubagentOutputPart();

  const markup = renderToStaticMarkup(
    createElement(SubagentOutputPart, {
      type: "data",
      name: "subagent-output",
      data: { agent: "evaluator", content: "评价阶段结论" },
      status: { type: "complete" },
    }),
  );
  assert.match(markup, /data-slot="subagent-message"/);
  assert.match(markup, /评价/);
  assert.match(markup, /评价阶段结论/);

  // 空内容零渲染
  const empty = renderToStaticMarkup(
    createElement(SubagentOutputPart, {
      type: "data",
      name: "subagent-output",
      data: { agent: "evaluator", content: "   " },
      status: { type: "complete" },
    }),
  );
  assert.equal(empty, "");
});

// —— AssistantMessageFooter(T7) ——

test("assistant footer renders citations and feedback buttons when wired", async () => {
  const { AssistantMessageFooter } = await loadFooter();

  const markup = renderToStaticMarkup(
    createElement(AssistantMessageFooter, {
      citations: [
        { chunk_id: "c-1", document_id: "doc-1", page: 2, source: "ml.pdf" },
      ],
      feedbackSessionId: "s-1",
      messageId: "2026-08-13T02:00:00Z",
      onFeedback: () => {},
    }),
  );

  assert.match(markup, /data-slot="assistant-message-footer"/);
  assert.match(markup, /data-slot="citation-list"/);
  assert.match(markup, /ml\.pdf/);
  assert.match(markup, /data-slot="feedback-up"/);
  assert.match(markup, /data-slot="feedback-down"/);
});

test("assistant footer degrades to nothing without citations or feedback", async () => {
  const { AssistantMessageFooter } = await loadFooter();

  const markup = renderToStaticMarkup(
    createElement(AssistantMessageFooter, { citations: null }),
  );
  assert.equal(markup, "");

  // 仅反馈接线(无引用)时只渲染按钮;citation-list 保持零渲染红线
  const feedbackOnly = renderToStaticMarkup(
    createElement(AssistantMessageFooter, {
      citations: null,
      feedbackSessionId: "s-1",
      onFeedback: () => {},
    }),
  );
  assert.match(feedbackOnly, /data-slot="feedback-up"/);
  assert.doesNotMatch(feedbackOnly, /data-slot="citation-list"/);
});
