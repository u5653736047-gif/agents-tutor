import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

import type { components } from "../contracts/api.generated";

const converterPath = new URL(
  "../lib/assistant/message-converter.ts",
  import.meta.url,
);

type Message = components["schemas"]["Message"];
type StreamEvent = components["schemas"]["StreamEvent"];
type Citation = components["schemas"]["Citation"];

async function loadConverter() {
  assert.ok(existsSync(converterPath), "missing message-converter lib");
  return import("../lib/assistant/message-converter");
}

// —— 测试夹具(契约单一数据源;StreamEvent 仅 event_type/sequence/session_id
// 必填,其余字段按用例覆盖) ——

function makeEvent(partial: Partial<StreamEvent> & Pick<StreamEvent, "event_type">): StreamEvent {
  return { sequence: 1, session_id: "s-1", ...partial };
}

function makeMessage(partial: Partial<Message> & Pick<Message, "role">): Message {
  return { content: "", ...partial };
}

function makeCitation(chunkId: string): Citation {
  return {
    chunk_id: chunkId,
    document_id: "doc-1",
    page: 3,
    source: "ml-textbook.pdf",
  };
}

function emptySlice(overrides: Record<string, unknown> = {}) {
  return {
    events: [] as StreamEvent[],
    isStreaming: false,
    messages: [] as Message[],
    references: null,
    streamingAgent: null,
    streamingMessage: null,
    ...overrides,
  };
}

type Part = { type: string; [key: string]: unknown };

function partsOf(message: { content: unknown }): Part[] {
  assert.ok(Array.isArray(message.content), "content must be a parts array");
  return message.content as Part[];
}

function customOf(message: { metadata?: unknown }): Record<string, unknown> {
  const metadata = message.metadata as { custom?: Record<string, unknown> };
  return metadata?.custom ?? {};
}

// —— 历史消息 ——

test("history user message converts to text part with attachments metadata", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const attachments = [
    { content_type: "image/png", file_id: "f-1", name: "a.png", size: 12 },
  ];
  const out = convertConversationToThreadMessages(
    emptySlice({
      messages: [
        makeMessage({
          attachments,
          content: "看图回答",
          created_at: "2026-08-13T01:00:00Z",
          role: "user",
        }),
      ],
    }),
  );

  assert.equal(out.length, 1);
  assert.equal(out[0]?.role, "user");
  assert.equal(out[0]?.id, "2026-08-13T01:00:00Z");
  const parts = partsOf(out[0]!);
  assert.deepEqual(parts, [{ type: "text", text: "看图回答" }]);
  assert.deepEqual(customOf(out[0]!).attachments, attachments);
});

test("history assistant message carries agent metadata and joinStrategy none", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      messages: [
        makeMessage({
          agent: "teaching_assistant",
          content: "讲解内容",
          role: "assistant",
        }),
      ],
    }),
  );

  assert.equal(out[0]?.role, "assistant");
  // 缺省 created_at 的历史消息退回位置 id(与 conversation-panel key 同源)
  assert.equal(out[0]?.id, "m-assistant-0");
  assert.equal(customOf(out[0]!).agent, "teaching_assistant");
  // 多智能体边界:禁止 runtime 合并连续助手消息
  assert.deepEqual(
    (out[0] as { convertConfig?: unknown }).convertConfig,
    { joinStrategy: "none" },
  );
});

test("history conversion memoizes by message object identity", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const message = makeMessage({ content: "稳定引用", role: "user" });
  const first = convertConversationToThreadMessages(
    emptySlice({ messages: [message] }),
  );
  const second = convertConversationToThreadMessages(
    emptySlice({ messages: [message] }),
  );
  assert.equal(first[0], second[0]);
});

// —— 过程事件 ——

test("reasoning events become reasoning parts, skipping blank content", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({ content: "先想", event_type: "reasoning", sequence: 1 }),
        makeEvent({ content: "   ", event_type: "reasoning", sequence: 2 }),
      ],
      isStreaming: true,
    }),
  );

  const parts = partsOf(out[0]!);
  assert.deepEqual(parts, [{ type: "reasoning", text: "先想" }]);
  assert.deepEqual(out[0]?.status, { type: "running" });
});

test("reasoning parts carry the producer agent via providerMetadata", async () => {
  const { PROVIDER_METADATA_NS, convertConversationToThreadMessages } =
    await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({
          agent: "learning_assistant",
          content: "助学思考",
          event_type: "reasoning",
          sequence: 1,
        }),
        // 无角色事件不携带元数据(缺省不伪造)
        makeEvent({ content: "匿名思考", event_type: "reasoning", sequence: 2 }),
      ],
      isStreaming: true,
    }),
  );

  const parts = partsOf(out[0]!);
  assert.equal(parts.length, 2);
  const metadata = (parts[0] as { providerMetadata?: Record<string, unknown> })
    .providerMetadata;
  assert.deepEqual(metadata?.[PROVIDER_METADATA_NS], {
    agent: "learning_assistant",
  });
  assert.equal(
    (parts[1] as { providerMetadata?: unknown }).providerMetadata,
    undefined,
  );
});

test("tool_call and tool_result pair into one evolving tool-call part", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({
          event_type: "tool_call",
          input_summary: "query=反向传播",
          sequence: 1,
          tool_call_id: "tc-1",
          tool_name: "search_knowledge",
        }),
        makeEvent({
          event_type: "tool_result",
          output_summary: "命中 3 条",
          sequence: 2,
          success: true,
          tool_call_id: "tc-1",
        }),
      ],
      isStreaming: true,
    }),
  );

  const parts = partsOf(out[0]!);
  assert.equal(parts.length, 1);
  assert.deepEqual(parts[0], {
    type: "tool-call",
    toolCallId: "tc-1",
    toolName: "search_knowledge",
    argsText: "query=反向传播",
    result: "命中 3 条",
    isError: false,
  });
});

test("failed tool_result marks isError; missing call degrades to orphan part", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({
          event_type: "tool_result",
          output_summary: "执行超时",
          sequence: 1,
          success: false,
          tool_call_id: "tc-missing",
          tool_name: "shell",
        }),
      ],
      isStreaming: true,
    }),
  );

  const parts = partsOf(out[0]!);
  assert.equal(parts.length, 1);
  assert.equal(parts[0]?.type, "tool-call");
  assert.equal(parts[0]?.isError, true);
  assert.equal(parts[0]?.result, "执行超时");
});

test("tool_output appends incremental content into the matching call result", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({
          event_type: "tool_call",
          sequence: 1,
          tool_call_id: "tc-1",
          tool_name: "shell",
        }),
        makeEvent({
          content: "第一行\n",
          event_type: "tool_output",
          sequence: 2,
          tool_call_id: "tc-1",
        }),
        makeEvent({
          content: "第二行",
          event_type: "tool_output",
          sequence: 3,
          tool_call_id: "tc-1",
        }),
        // 孤儿增量(无匹配 call)按语义丢弃,不产生 part
        makeEvent({
          content: "孤儿",
          event_type: "tool_output",
          sequence: 4,
          tool_call_id: "tc-none",
        }),
      ],
      isStreaming: true,
    }),
  );

  const parts = partsOf(out[0]!);
  assert.equal(parts.length, 1);
  assert.equal(parts[0]?.result, "第一行\n第二行");
});

test("agent_switch emits deduped data parts only on role change", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({ agent: "supervisor", event_type: "agent_switch", sequence: 1 }),
        makeEvent({ agent: "supervisor", event_type: "agent_switch", sequence: 2 }),
        makeEvent({
          agent: "learning_assistant",
          event_type: "agent_switch",
          sequence: 3,
        }),
      ],
      isStreaming: true,
    }),
  );

  const parts = partsOf(out[0]!);
  assert.deepEqual(parts, [
    { type: "data", name: "agent-switch", data: { agent: "supervisor" } },
    { type: "data", name: "agent-switch", data: { agent: "learning_assistant" } },
  ]);
});

test("worker message_delta becomes subagent-output card; supervisor is skipped", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        // supervisor 的 delta 由 streamingMessage 主路径承载,不进 events 卡
        makeEvent({
          agent: "supervisor",
          content: "督学文本",
          event_type: "message_delta",
          sequence: 1,
        }),
        makeEvent({
          agent: "teaching_assistant",
          content: "助教阶段稿",
          event_type: "message_delta",
          sequence: 2,
        }),
      ],
      isStreaming: true,
      streamingMessage: makeMessage({
        agent: "supervisor",
        content: "督学文本",
        role: "assistant",
      }),
    }),
  );

  const parts = partsOf(out[0]!);
  assert.deepEqual(parts[0], {
    type: "data",
    name: "subagent-output",
    data: { agent: "teaching_assistant", content: "助教阶段稿" },
  });
  // 末尾是 supervisor 主路径的权威文本
  assert.deepEqual(parts[1], { type: "text", text: "督学文本" });
});

test("unmapped event types are tolerated and skipped", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({ content: "阶段提示", event_type: "thinking", sequence: 1 }),
        makeEvent({ event_type: "done", sequence: 2 }),
        makeEvent({ error_code: "model_call_failed", event_type: "error", sequence: 3 }),
      ],
      isStreaming: true,
      streamingMessage: makeMessage({ content: "正文", role: "assistant" }),
    }),
  );

  const parts = partsOf(out[0]!);
  assert.deepEqual(parts, [{ type: "text", text: "正文" }]);
});

// —— 在飞消息与引用 ——

test("no activity produces no extra message beyond history", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const out = convertConversationToThreadMessages(
    emptySlice({ messages: [makeMessage({ content: "问", role: "user" })] }),
  );
  assert.equal(out.length, 1);
});

test("completed run keeps the final message without running status", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const citations = [makeCitation("c-1")];
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({ content: "思考", event_type: "reasoning", sequence: 1 }),
      ],
      isStreaming: false,
      references: citations,
      streamingAgent: "evaluator",
      streamingMessage: makeMessage({
        agent: "evaluator",
        content: "最终回答",
        created_at: "2026-08-13T02:00:00Z",
        role: "assistant",
      }),
    }),
  );

  assert.equal(out.length, 1);
  const parts = partsOf(out[0]!);
  assert.deepEqual(parts, [
    { type: "reasoning", text: "思考" },
    { type: "text", text: "最终回答" },
  ]);
  assert.equal(out[0]?.status, undefined);
  assert.equal(out[0]?.id, "2026-08-13T02:00:00Z");
  assert.deepEqual(customOf(out[0]!).citations, citations);
  assert.equal(customOf(out[0]!).agent, "evaluator");
});

test("references attach to the last history assistant message after reload", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const citations = [makeCitation("c-9")];
  const out = convertConversationToThreadMessages(
    emptySlice({
      messages: [
        makeMessage({ content: "问", created_at: "t1", role: "user" }),
        makeMessage({ content: "答", created_at: "t2", role: "assistant" }),
      ],
      references: citations,
    }),
  );

  assert.equal(out.length, 2);
  assert.deepEqual(customOf(out[1]!).citations, citations);
  assert.equal(customOf(out[0]!).citations, undefined);
});

test("process parts fold into the last assistant message after session reload", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const citations = [makeCitation("c-2")];
  const out = convertConversationToThreadMessages(
    emptySlice({
      events: [
        makeEvent({ content: "复盘思考", event_type: "reasoning", sequence: 1 }),
        makeEvent({
          event_type: "tool_call",
          sequence: 2,
          tool_call_id: "tc-1",
          tool_name: "search_knowledge",
        }),
      ],
      messages: [
        makeMessage({ content: "问", created_at: "t1", role: "user" }),
        makeMessage({ content: "权威回答", created_at: "t2", role: "assistant" }),
      ],
      references: citations,
    }),
  );

  assert.equal(out.length, 2);
  const parts = partsOf(out[1]!);
  // 过程 parts 在前、权威文本在后,思维链刷新后可恢复
  assert.deepEqual(parts[0], { type: "reasoning", text: "复盘思考" });
  assert.equal(parts[1]?.type, "tool-call");
  assert.deepEqual(parts[2], { type: "text", text: "权威回答" });
  assert.deepEqual(customOf(out[1]!).citations, citations);
});

// —— 性能:1000 事件转换单帧内完成(16ms 预算) ——

test("converting 1000 events stays within one frame budget", async () => {
  const { convertConversationToThreadMessages } = await loadConverter();
  const events: StreamEvent[] = [];
  for (let index = 0; index < 1000; index += 1) {
    if (index % 3 === 0) {
      events.push(
        makeEvent({ content: `r${index}`, event_type: "reasoning", sequence: index }),
      );
    } else if (index % 3 === 1) {
      events.push(
        makeEvent({
          event_type: "tool_call",
          sequence: index,
          tool_call_id: `tc-${index}`,
          tool_name: "search_knowledge",
        }),
      );
    } else {
      events.push(
        makeEvent({
          event_type: "tool_result",
          output_summary: "ok",
          sequence: index,
          tool_call_id: `tc-${index - 1}`,
        }),
      );
    }
  }

  const start = performance.now();
  const out = convertConversationToThreadMessages(
    emptySlice({ events, isStreaming: true }),
  );
  const elapsed = performance.now() - start;

  assert.equal(out.length, 1);
  assert.ok(
    elapsed < 16,
    `expected conversion under 16ms, took ${elapsed.toFixed(2)}ms`,
  );
});
