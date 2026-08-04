import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";
import type { ApiClient } from "../lib/api-client";

const storePath = new URL("../stores/chat-store.ts", import.meta.url);
const apiClientPath = new URL("../lib/api-client.ts", import.meta.url);

async function loadChatStore() {
  assert.ok(existsSync(storePath), "missing Zustand chat store");
  return import("../stores/chat-store");
}

async function loadApiClient() {
  assert.ok(existsSync(apiClientPath), "missing generated-type API client");
  return import("../lib/api-client");
}

const session = {
  archived: false,
  created_at: "2026-08-03T00:00:00Z",
  session_id: "session-1",
  user_id: "demo-user",
};

const assistantMessage = {
  agent: "supervisor" as const,
  content: "你好！",
  role: "assistant" as const,
};

type StreamChatStub = (options: {
  message: string;
  onEvent(event: unknown): void;
  sessionId: string;
}) => Promise<void>;

async function createStreamStore(overrides: {
  getSessionMessages?: () => Promise<unknown[]>;
  streamChat?: StreamChatStub;
}) {
  const { createChatStore } = await loadChatStore();
  return createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: (overrides.getSessionMessages ??
      (async () => [])) as ApiClient["getSessionMessages"],
    listSessions: async () => [session],
    sendChat: async () => ({ events: [], session_id: session.session_id }),
    streamChat: overrides.streamChat,
  });
}

test("streaming lifecycle merges the final message and tracks the active agent", async () => {
  const events = [
    {
      agent: "supervisor",
      event_type: "thinking",
      sequence: 1,
      session_id: "session-1",
    },
    {
      agent: "supervisor",
      event_type: "tool_call",
      sequence: 2,
      session_id: "session-1",
      tool_name: "web_search",
    },
    {
      agent: "teaching_assistant",
      event_type: "agent_switch",
      sequence: 3,
      session_id: "session-1",
    },
    {
      agent: "teaching_assistant",
      content: "流式回答",
      event_type: "message_end",
      sequence: 4,
      session_id: "session-1",
    },
    { event_type: "done", sequence: 5, session_id: "session-1" },
  ];

  const observations: Array<{
    isStreaming: boolean;
    streamingAgent: unknown;
    streamingMessage: unknown;
  }> = [];
  let messagesAtHistoryFetch: unknown = null;

  const store = await createStreamStore({
    // 拉权威历史的一刻读取 messages:验证流式消息已在拉取前并入列表
    getSessionMessages: async () => {
      messagesAtHistoryFetch = store.getState().messages;
      return [assistantMessage];
    },
    streamChat: async ({ onEvent }) => {
      for (const event of events) {
        onEvent(event);
        observations.push({
          isStreaming: store.getState().isStreaming,
          streamingAgent: store.getState().streamingAgent,
          streamingMessage: store.getState().streamingMessage,
        });
      }
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("请解释流式概念");

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.equal(state.streamingAgent, null);
  assert.equal(state.streamingMessage, null);
  assert.deepEqual(state.messages, [assistantMessage]);
  // 摘要事件(tool_call)追加进 events 列表
  assert.deepEqual(
    state.events.map((event) => event.event_type),
    ["tool_call"],
  );
  // 生命周期:流式期间 isStreaming 一直为 true
  assert.ok(observations.every((item) => item.isStreaming === true));
  // thinking 与 agent_switch 都更新流式徽章,agent_switch 后为新 agent
  assert.equal(observations[0]?.streamingAgent, "supervisor");
  assert.equal(observations[2]?.streamingAgent, "teaching_assistant");
  // message_end 后 streamingMessage 持有最终内容
  assert.equal(
    (observations[3]?.streamingMessage as { content?: string } | null)?.content,
    "流式回答",
  );
  // 收尾时流式消息已并入消息列表(拉权威历史前一刻可见)
  assert.deepEqual(
    (messagesAtHistoryFetch as Array<{ content: string }>).map((item) => item.content),
    ["流式回答"],
  );
});

test("an error event sets runError and the stream still finishes cleanly", async () => {
  const store = await createStreamStore({
    streamChat: async ({ onEvent }) => {
      onEvent({
        agent: "supervisor",
        error_code: "internal_error",
        event_type: "error",
        sequence: 1,
        session_id: "session-1",
      });
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("会失败的问题");

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.deepEqual(state.runError, {
    agent: "supervisor",
    error_code: "internal_error",
    message: "The request could not be completed.",
  });
});

test("a stream failure keeps received content and records requestError", async () => {
  const { ApiClientError } = await loadApiClient();
  const failure = new ApiClientError("流式中断。", { code: null, status: null });
  const store = await createStreamStore({
    streamChat: async ({ onEvent }) => {
      onEvent({
        agent: "supervisor",
        event_type: "thinking",
        sequence: 1,
        session_id: "session-1",
      });
      onEvent({
        content: "部分内容",
        event_type: "message_end",
        sequence: 2,
        session_id: "session-1",
      });
      throw failure;
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("会中断的请求");

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.equal(state.requestError, failure);
  assert.equal((state.streamingMessage as { content: string } | null)?.content, "部分内容");
});

test("switching sessions during a stream discards stale events", async () => {
  let emitEvent: ((event: Record<string, unknown>) => void) | undefined;
  let markStreamStarted: (() => void) | undefined;
  let resolveStream: (() => void) | undefined;
  const streamStarted = new Promise<void>((resolve) => {
    markStreamStarted = resolve;
  });

  const store = await createStreamStore({
    streamChat: async ({ onEvent }) => {
      emitEvent = onEvent;
      markStreamStarted?.();
      await new Promise<void>((resolve) => {
        resolveStream = resolve;
      });
    },
  });

  store.getState().selectSession("session-a");
  const streaming = store.getState().streamSendMessage("等待流式响应");
  await streamStarted;

  store.getState().selectSession("session-b");
  assert.equal(store.getState().isStreaming, false);

  // 旧会话的流事件到达,不应污染新会话状态
  emitEvent?.({
    agent: "teaching_assistant",
    event_type: "agent_switch",
    sequence: 1,
    session_id: "session-a",
  });
  emitEvent?.({
    content: "旧会话的流式内容",
    event_type: "message_end",
    sequence: 2,
    session_id: "session-a",
  });
  assert.equal(store.getState().streamingAgent, null);
  assert.equal(store.getState().streamingMessage, null);
  assert.deepEqual(store.getState().messages, []);

  resolveStream?.();
  await streaming;

  assert.equal(store.getState().currentSessionId, "session-b");
  assert.equal(store.getState().isStreaming, false);
  assert.equal(store.getState().requestError, null);
  assert.deepEqual(store.getState().messages, []);
});

test("streamSendMessage without a session records a request error and skips the client", async () => {
  let streamCalled = false;
  const store = await createStreamStore({
    streamChat: async () => {
      streamCalled = true;
    },
  });

  await store.getState().streamSendMessage("消息");

  assert.equal(store.getState().requestError?.message, "请先选择会话。");
  assert.equal(store.getState().isStreaming, false);
  assert.equal(streamCalled, false);
});
