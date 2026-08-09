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
  // 安全思考摘要、工具调用与 Agent 切换都进入协作时间线。
  assert.deepEqual(
    state.events.map((event) => event.event_type),
    ["thinking", "tool_call", "agent_switch"],
  );
  // 生命周期:流式期间 isStreaming 一直为 true
  assert.ok(observations.every((item) => item.isStreaming === true));
  // thinking 与 agent_switch 都更新流式徽章,agent_switch 后为新 agent
  assert.equal(observations[0]?.streamingAgent, "supervisor");
  assert.equal(observations[2]?.streamingAgent, "teaching_assistant");
  // D2-T2:agent_switch 同时更新 currentAgent(活跃 Agent 高亮数据源)
  assert.equal(state.currentAgent, "teaching_assistant");
  // message_end 后 streamingMessage 持有最终内容
  assert.equal(
    (observations[3]?.streamingMessage as { content?: string } | null)?.content,
    "流式回答",
  );
  // 收尾时流式消息已并入消息列表(拉权威历史前一刻可见)。
  // UX-20260807#1:乐观用户消息在流式发起前已追加,此时位于列表首位。
  assert.deepEqual(
    (messagesAtHistoryFetch as Array<{ content: string }>).map((item) => item.content),
    ["请解释流式概念", "流式回答"],
  );
});

test("message deltas grow the supervisor bubble and coalesce subagent output", async () => {
  const supervisorSnapshots: string[] = [];
  const store = await createStreamStore({
    getSessionMessages: async () => [assistantMessage],
    streamChat: async ({ onEvent }) => {
      onEvent({
        agent: "supervisor",
        content: "流",
        event_type: "message_delta",
        message_id: "supervisor-answer",
        sequence: 1,
        session_id: "session-1",
      });
      supervisorSnapshots.push(store.getState().streamingMessage?.content ?? "");
      onEvent({
        agent: "supervisor",
        content: "式",
        event_type: "message_delta",
        message_id: "supervisor-answer",
        sequence: 2,
        session_id: "session-1",
      });
      supervisorSnapshots.push(store.getState().streamingMessage?.content ?? "");
      onEvent({
        agent: "learning_assistant",
        content: "子",
        event_type: "message_delta",
        message_id: "worker-answer",
        sequence: 3,
        session_id: "session-1",
      });
      onEvent({
        agent: "learning_assistant",
        content: "任务",
        event_type: "message_delta",
        message_id: "worker-answer",
        sequence: 4,
        session_id: "session-1",
      });
      onEvent({
        agent: "supervisor",
        content: "流式完成",
        event_type: "message_end",
        sequence: 5,
        session_id: "session-1",
      });
      onEvent({ event_type: "done", sequence: 6, session_id: "session-1" });
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("解释流式");

  assert.deepEqual(supervisorSnapshots, ["流", "流式"]);
  const workerMessages = store
    .getState()
    .events.filter((event) => event.event_type === "message_delta");
  assert.equal(workerMessages.length, 1);
  assert.equal("content" in workerMessages[0]! ? workerMessages[0].content : null, "子任务");
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
  // D2-T2:旧流的事件不污染新会话的 currentAgent(切会话已清空)
  assert.equal(store.getState().currentAgent, null);
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
  // D4-T3:调用方取消信号(store 透传 StreamChatOptions.signal)
  signal?: AbortSignal;
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

test("streamSendMessage never resends through the sync channel after retries are exhausted", async () => {
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
  await store.getState().streamSendMessage("不能自动重发的请求");

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.equal(sendChatCalls, 0, "a failed stream must not execute the same task again");
  assert.equal(state.degradedNotice, null);
  assert.equal(state.requestError, failure);
  assert.deepEqual(
    state.messages.map((message) => message.content),
    ["不能自动重发的请求"],
  );
});

test("concurrent stream submissions start only one run", async () => {
  let calls = 0;
  let release: (() => void) | undefined;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const store = await createRetryStreamStore({
    streamChatWithRetry: async ({ onEvent }) => {
      calls += 1;
      await pending;
      onEvent({ event_type: "done", sequence: 1, session_id: session.session_id });
    },
  });

  store.getState().selectSession(session.session_id);
  const first = store.getState().streamSendMessage("只执行一次");
  const second = store.getState().streamSendMessage("只执行一次");

  assert.equal(calls, 1);
  release?.();
  await Promise.all([first, second]);
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
  // UX-20260807#1:乐观用户消息在流式发起前已追加,此时位于列表首位。
  assert.deepEqual(
    (messagesAtHistoryFetch as Array<{ content: string }>).map((item) => item.content),
    ["会中断的请求", "重连后的回答"],
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

test("streamSendMessage stores references from the message_end event", async () => {
  // D3-T5:流式主通道的引用由 message_end 事件的 citations 携带
  // (review blocking 修复:此前流式路径从不设置 references,引用在
  // 真实使用中不可达)。未携带时保持 null。
  const store = await createRetryStreamStore({
    streamChatWithRetry: async ({ onEvent }) => {
      onEvent({
        agent: "learning_assistant",
        citations: [
          {
            chunk_id: "ml-zhouzhihua:88:0:500",
            document_id: "ml-zhouzhihua",
            page: 88,
            source: "ml-zhouzhihua",
          },
        ],
        content: "带引用的回答",
        event_type: "message_end",
        sequence: 1,
        session_id: "session-1",
      });
      onEvent({ event_type: "done", sequence: 2, session_id: "session-1" });
    },
  });

  store.getState().selectSession("session-1");
  await store.getState().streamSendMessage("请检索");

  const state = store.getState();
  assert.deepEqual(state.references, [
    {
      chunk_id: "ml-zhouzhihua:88:0:500",
      document_id: "ml-zhouzhihua",
      page: 88,
      source: "ml-zhouzhihua",
    },
  ]);
  assert.equal(state.isStreaming, false);
});

// ── D4-T3 停止生成:取消当前流 ──────────────────────────────────

test("cancelStreaming aborts the active stream and finishes cleanly", async () => {
  // D4-T3:abort 后 streamChatWithRetry 对调用方取消静默返回(D1-T3
  // 语义),streamSendMessage 走正常收尾路径:不抛错、isStreaming
  // 复位、已收到的流式内容保留。
  let receivedSignal: AbortSignal | undefined;
  let markStreamStarted: (() => void) | undefined;
  const streamStarted = new Promise<void>((resolve) => {
    markStreamStarted = resolve;
  });

  const store = await createRetryStreamStore({
    streamChatWithRetry: async ({ onEvent, signal }) => {
      receivedSignal = signal;
      onEvent({
        content: "部分内容",
        event_type: "message_end",
        sequence: 1,
        session_id: session.session_id,
      });
      markStreamStarted?.();
      // 挂起直到调用方 abort(真实通道里由 fetch 读取循环响应)
      await new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve(), { once: true });
      });
    },
  });

  store.getState().selectSession(session.session_id);
  const streaming = store.getState().streamSendMessage("可取消的请求");
  await streamStarted;

  store.getState().cancelStreaming();
  assert.equal(receivedSignal?.aborted, true);

  // abort 后 streamSendMessage 正常收尾:不抛、状态复位
  await streaming;
  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.equal(state.requestError, null);
  assert.equal(state.degradedNotice, null);
  assert.equal(state.streamingMessage, null);
});

test("cancelStreaming without an active stream is a no-op", async () => {
  const store = await createRetryStreamStore({});

  store.getState().selectSession(session.session_id);
  assert.doesNotThrow(() => store.getState().cancelStreaming());
  assert.equal(store.getState().isStreaming, false);
});

test("cancelStreaming aborts only the active stream; a later stream is unaffected", async () => {
  // D4-T3:controller 存于 store 实例且流结束后清理(按引用比对),
  // 取消旧流不会影响之后的新流。
  const signals: AbortSignal[] = [];
  let markStreamStarted: (() => void) | undefined;
  const streamStarted = new Promise<void>((resolve) => {
    markStreamStarted = resolve;
  });

  const store = await createRetryStreamStore({
    streamChatWithRetry: async ({ onEvent, signal }) => {
      signals.push(signal as AbortSignal);
      if (signals.length === 1) {
        markStreamStarted?.();
        // 第一个流挂起,等待调用方 abort
        await new Promise<void>((resolve) => {
          signal?.addEventListener("abort", () => resolve(), { once: true });
        });
        return;
      }
      onEvent({ event_type: "done", sequence: 2, session_id: session.session_id });
    },
  });

  store.getState().selectSession(session.session_id);
  const first = store.getState().streamSendMessage("第一个流");
  await streamStarted;

  store.getState().cancelStreaming();
  assert.equal(signals[0]?.aborted, true);
  await first;
  assert.equal(store.getState().isStreaming, false);

  // 新流使用全新 controller,不受旧流取消影响
  await store.getState().streamSendMessage("第二个流");
  assert.equal(signals.length, 2);
  assert.equal(signals[1]?.aborted, false);
});

test("switching sessions aborts the active stream and stale events do not leak back", async () => {
  // review 修正回归:selectSession 切会话时 abort 活跃流——旧流继续
  // 后台跑完浪费算力,且「切走再切回」时旧流剩余事件会重新通过会话
  // 守卫写回污染新状态。abort 后旧流的收尾被会话守卫拦截。
  let receivedSignal: AbortSignal | undefined;
  let markStreamStarted: (() => void) | undefined;
  const streamStarted = new Promise<void>((resolve) => {
    markStreamStarted = resolve;
  });

  const store = await createRetryStreamStore({
    streamChatWithRetry: async ({ onEvent, signal }) => {
      receivedSignal = signal;
      onEvent({
        content: "旧流的部分内容",
        event_type: "message_end",
        sequence: 1,
        session_id: session.session_id,
      });
      markStreamStarted?.();
      await new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve(), { once: true });
      });
    },
  });

  store.getState().selectSession(session.session_id);
  const streaming = store.getState().streamSendMessage("旧会话请求");
  await streamStarted;

  // 流进行中切会话:活跃 controller 被 abort,旧流剩余事件被会话
  // 守卫拦截,不写回(已切换到新会话的状态)。
  store.getState().selectSession("session-b");
  assert.equal(receivedSignal?.aborted, true);
  await streaming;

  const state = store.getState();
  assert.equal(state.currentSessionId, "session-b");
  assert.equal(state.messages.length, 0); // 旧流内容未污染新会话
  assert.equal(state.streamingMessage, null);
  assert.equal(state.isStreaming, false);
});

test("an abort racing an ApiClientError does not trigger the sync fallback", async () => {
  // review 修正回归:用户点「停止生成」(abort)恰逢后端错误响应时,
  // stream-client 的 catch 先判 ApiClientError 再判 signal.aborted,
  // 错误可能透传进 store——catch 的 abort 短路必须拦截,否则误走
  // 降级分支会 sendMessage 重发同一条已送达消息。
  const { ApiClientError } = await loadApiClient();
  let sendChatCalls = 0;
  let markStreamStarted: (() => void) | undefined;
  const streamStarted = new Promise<void>((resolve) => {
    markStreamStarted = resolve;
  });

  const store = await createRetryStreamStore({
    sendChat: async () => {
      sendChatCalls += 1;
      return { events: [], message: assistantMessage, session_id: session.session_id };
    },
    streamChatWithRetry: async ({ signal }) => {
      markStreamStarted?.();
      // 模拟「abort 与错误竞争」:先等待 abort,再抛 ApiClientError
      // (stream-client 实际行为:catch 先命中 ApiClientError 分支)。
      await new Promise<void>((resolve) => {
        signal?.addEventListener("abort", () => resolve(), { once: true });
      });
      throw new ApiClientError("服务错误。", { code: "internal_error", status: 500 });
    },
  });

  store.getState().selectSession(session.session_id);
  const streaming = store.getState().streamSendMessage("会竞争的消息");
  await streamStarted;

  store.getState().cancelStreaming();
  await streaming;

  const state = store.getState();
  assert.equal(state.isStreaming, false);
  assert.equal(state.degradedNotice, null); // abort 短路:不降级
  assert.equal(sendChatCalls, 0); // 不重发
});
