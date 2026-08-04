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

// ── D1-T3 断线重连与消息补发:重试通道与同步降级 ────────────────────

type StreamChatWithRetryStub = (options: {
  maxRetries?: number;
  message: string;
  onEvent(event: unknown): void;
  sessionId: string;
}) => Promise<void>;

// 注入 streamChatWithRetry 的 store 构造(重试语义由 stub 自持,
// store 只负责委托与收尾,见 streamSendMessage 的 D1-T3 分支)。
async function createRetryStreamStore(overrides: {
  getSessionMessages?: () => Promise<unknown[]>;
  sendChat?: () => Promise<unknown>;
  streamChatWithRetry?: StreamChatWithRetryStub;
}) {
  const { createChatStore } = await loadChatStore();
  return createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: (overrides.getSessionMessages ??
      (async () => [])) as ApiClient["getSessionMessages"],
    listSessions: async () => [session],
    sendChat: (overrides.sendChat ??
      (async () => ({ events: [], session_id: session.session_id }))) as ApiClient["sendChat"],
    streamChatWithRetry: overrides.streamChatWithRetry,
  });
}

test("streamSendMessage falls back to the sync channel after retries are exhausted", async () => {
  const { ApiClientError } = await loadApiClient();
  const failure = new ApiClientError("流式通道重试耗尽。", { code: null, status: null });
  let sendChatCalls = 0;
  const store = await createRetryStreamStore({
    getSessionMessages: async () => [assistantMessage],
    sendChat: async () => {
      sendChatCalls += 1;
      return { events: [], message: assistantMessage, session_id: session.session_id };
    },
    streamChatWithRetry: async () => {
      throw failure;
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("会降级的请求");

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  // 降级提示被设置,且同步通道确实被调用(sendMessage 拉全量后消息一致)。
  assert.equal(
    state.degradedNotice,
    "网络不稳定,已切换到同步通道,消息可能缺少过程事件。",
  );
  assert.equal(sendChatCalls, 1);
  assert.deepEqual(state.messages, [assistantMessage]);
});

test("streamSendMessage finishes normally via the retry channel after internal recovery", async () => {
  // 语义:重试循环在 streamChatWithRetry(stream-client)内部,store
  // 只调用一次委托方法——stub 模拟「内部已恢复」:首次尝试收到
  // seq=5 后中断,重试成功 message_end + done 收尾(单次委托调用
  // 内自持)。store 侧断言:正常收尾(不降级、不报错、消息并入)。
  let messagesAtHistoryFetch: unknown = null;
  const store = await createRetryStreamStore({
    // 拉权威历史的一刻读取 messages:验证流式消息已并入
    getSessionMessages: async () => {
      messagesAtHistoryFetch = store.getState().messages;
      return [assistantMessage];
    },
    streamChatWithRetry: async ({ fromSequence, onEvent }) => {
      // 首次尝试:收到 seq=5 的事件后流中断,内部重试续传
      // (fromSequence 由 stream-client 维护,stub 只需演示语义)。
      assert.equal(fromSequence, undefined); // store 不传 fromSequence(默认 0)
      onEvent({
        agent: "supervisor",
        event_type: "thinking",
        sequence: 5,
        session_id: session.session_id,
      });
      onEvent({
        content: "重连后的回答",
        event_type: "message_end",
        sequence: 6,
        session_id: session.session_id,
      });
      onEvent({ event_type: "done", sequence: 7, session_id: session.session_id });
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("会中断的请求");

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.equal(state.requestError, null);
  assert.equal(state.degradedNotice, null);
  // 流式消息并入消息列表后再拉权威历史,消息一致。
  assert.deepEqual(
    (messagesAtHistoryFetch as Array<{ content: string }>).map((item) => item.content),
    ["重连后的回答"],
  );
  assert.deepEqual(state.messages, [assistantMessage]);
});

test("a history fetch failure after a successful stream does not trigger the sync fallback", async () => {
  // review 修正回归:流式成功后的拉权威历史失败 ≠ 流式通道失败——
  // 若落入降级分支会 sendMessage 重发同一条已送达消息,造成历史重复。
  // 正确行为:只设 requestError,不降级、不重发(sendChat 不被调用)。
  const { ApiClientError } = await loadApiClient();
  const historyFailure = new ApiClientError("拉取历史失败。", {
    code: null,
    status: null,
  });
  let sendChatCalls = 0;
  const store = await createRetryStreamStore({
    getSessionMessages: async () => {
      throw historyFailure;
    },
    sendChat: async () => {
      sendChatCalls += 1;
      return { events: [], message: assistantMessage, session_id: session.session_id };
    },
    streamChatWithRetry: async ({ onEvent }) => {
      onEvent({
        content: "已送达的回答",
        event_type: "message_end",
        sequence: 1,
        session_id: session.session_id,
      });
      onEvent({ event_type: "done", sequence: 2, session_id: session.session_id });
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("已送达的请求");

  const state = store.getState();
  assert.equal(sendChatCalls, 0, "history failure must not re-send the message");
  assert.equal(state.degradedNotice, null);
  assert.ok(state.requestError instanceof ApiClientError);
  assert.equal(state.isStreaming, false);
});
