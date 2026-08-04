import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

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

const userMessage = {
  agent: null,
  content: "请解释概念",
  role: "user" as const,
};

const event = {
  event_type: "message_end" as const,
  sequence: 1,
};

test("the chat store keeps response messages and events in separate fields", async () => {
  const { createChatStore } = await loadChatStore();
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [event],
      message: assistantMessage,
      session_id: session.session_id,
    }),
  });

  await store.getState().refreshSessions();
  store.getState().selectSession(session.session_id);
  await store.getState().sendMessage("请解释概念");

  const state = store.getState();
  assert.deepEqual(state.sessions, [session]);
  assert.deepEqual(state.messages, [userMessage, assistantMessage]);
  assert.deepEqual(state.events, [event]);
  assert.equal(state.isSending, false);
  assert.equal(state.requestError, null);});

test("the chat store records a client failure without optimistic messages", async () => {
  const { ApiClientError } = await loadApiClient();
  const { createChatStore } = await loadChatStore();
  const failure = new ApiClientError("服务不可用。", { code: null, status: null });
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => {
      throw failure;
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().sendMessage("会失败的请求");

  const state = store.getState();
  assert.deepEqual(state.messages, []);
  assert.deepEqual(state.events, []);
  assert.equal(state.isSending, false);
  assert.equal(state.requestError, failure);
});

test("switching sessions clears a pending send from the previous session", async () => {
  const { createChatStore } = await loadChatStore();
  let resolveChat: ((value: { events: []; session_id: string }) => void) | undefined;
  let markChatStarted: (() => void) | undefined;
  const chatStarted = new Promise<void>((resolve) => {
    markChatStarted = resolve;
  });
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => {
      markChatStarted?.();
      return new Promise((resolve) => {
        resolveChat = resolve;
      });
    },
  });

  store.getState().selectSession("session-a");
  const sending = store.getState().sendMessage("等待响应");
  await chatStarted;
  store.getState().selectSession("session-b");

  assert.equal(store.getState().isSending, false);
  resolveChat?.({ events: [], session_id: "session-a" });
  await sending;
  assert.equal(store.getState().currentSessionId, "session-b");
  assert.equal(store.getState().isSending, false);
  assert.equal(store.getState().requestError, null);
});

test("a previous session failure does not overwrite the current session error", async () => {
  const { ApiClientError } = await loadApiClient();
  const { createChatStore } = await loadChatStore();
  const failure = new ApiClientError("会话 A 失败。", { code: null, status: null });
  let rejectChat: ((reason?: unknown) => void) | undefined;
  let markChatStarted: (() => void) | undefined;
  const chatStarted = new Promise<void>((resolve) => {
    markChatStarted = resolve;
  });
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => {
      markChatStarted?.();
      return new Promise((_resolve, reject) => {
        rejectChat = reject;
      });
    },
  });

  store.getState().selectSession("session-a");
  const sending = store.getState().sendMessage("会失败的请求");
  await chatStarted;
  store.getState().selectSession("session-b");
  rejectChat?.(failure);
  await sending;

  assert.equal(store.getState().currentSessionId, "session-b");
  assert.equal(store.getState().isSending, false);
  assert.equal(store.getState().requestError, null);
});

test("a new run resets events so timelines do not interleave across runs", async () => {
  // review 修正回归:events 是「本轮事件」(ChatResponse.events 按
  // previous_sequence 过滤的增量语义),每轮 sequence 从 1 起——若跨
  // run 累积,协作面板时间线会交错、key 与展开状态会撞号。
  const { createChatStore } = await loadChatStore();
  let run = 0;
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => {
      run += 1;
      return {
        events: [
          { event_type: "tool_call" as const, sequence: run, tool_name: "search" },
        ],
        message: assistantMessage,
        session_id: session.session_id,
      };
    },
  });

  await store.getState().refreshSessions();
  store.getState().selectSession(session.session_id);
  await store.getState().sendMessage("第一轮");
  assert.deepEqual(store.getState().events, [
    { event_type: "tool_call", sequence: 1, tool_name: "search" },
  ]);

  await store.getState().sendMessage("第二轮");
  // 第二轮开始时 events 已重置:只含本轮事件,不残留第一轮的 sequence=1
  assert.deepEqual(store.getState().events, [
    { event_type: "tool_call", sequence: 2, tool_name: "search" },
  ]);
});

// D2-T3:审批决策(确认/拒绝)——————————————————————————
const pendingHandoff = {
  interrupt_id: "interrupt-1",
  request: {
    plan_step_sequence: 2,
    target_agent: "teaching_assistant" as const,
    task_content: "请整理学习笔记。",
  },
};

test("decideHandoff confirms and merges the response", async () => {
  const { createChatStore } = await loadChatStore();
  const decided: string[] = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    decideHandoff: async (_sessionId, decision) => {
      decided.push(decision.action);
      return {
        current_agent: "learning_assistant" as const,
        events: [
          { event_type: "tool_call" as const, sequence: 1, tool_name: "search" },
        ],
        message: assistantMessage,
        pending_handoff: null,
        session_id: session.session_id,
      };
    },
    getPendingHandoff: async () => ({
      pending_handoff: null,
      session_id: session.session_id,
    }),
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [],
      message: assistantMessage,
      session_id: session.session_id,
    }),
  });

  store.getState().selectSession(session.session_id);
  store.setState({ pendingHandoff });
  await store.getState().decideHandoff("confirm");

  const state = store.getState();
  // 动作透传 + 全量历史覆盖 + events 重置为本轮增量 + pending 清除
  assert.deepEqual(decided, ["confirm"]);
  assert.deepEqual(state.messages, [userMessage, assistantMessage]);
  assert.deepEqual(state.events, [
    { event_type: "tool_call", sequence: 1, tool_name: "search" },
  ]);
  assert.equal(state.pendingHandoff, null);
  assert.equal(state.currentAgent, "learning_assistant");
  assert.equal(state.isDecidingHandoff, false);
  assert.equal(state.requestError, null);
});

test("decideHandoff rejects and clears the pending handoff", async () => {
  const { createChatStore } = await loadChatStore();
  const decided: string[] = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    decideHandoff: async (_sessionId, decision) => {
      decided.push(decision.action);
      return {
        events: [],
        message: assistantMessage,
        pending_handoff: null,
        session_id: session.session_id,
      };
    },
    getPendingHandoff: async () => ({
      pending_handoff: null,
      session_id: session.session_id,
    }),
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [],
      message: assistantMessage,
      session_id: session.session_id,
    }),
  });

  store.getState().selectSession(session.session_id);
  store.setState({ pendingHandoff });
  await store.getState().decideHandoff("reject");

  assert.deepEqual(decided, ["reject"]);
  assert.equal(store.getState().pendingHandoff, null);
  assert.equal(store.getState().isDecidingHandoff, false);
  assert.equal(store.getState().requestError, null);
});

test("decideHandoff refreshes pending when another client handled it", async () => {
  const { ApiClientError } = await loadApiClient();
  const { createChatStore } = await loadChatStore();
  const freshPending = {
    interrupt_id: "interrupt-2",
    request: {
      plan_step_sequence: 4,
      target_agent: "evaluator" as const,
      task_content: "评估新任务。",
    },
  };
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    decideHandoff: async () => {
      throw new ApiClientError("No handoff is pending for this session.", {
        code: "handoff_not_pending",
        status: 409,
      });
    },
    getPendingHandoff: async () => ({
      pending_handoff: freshPending,
      session_id: session.session_id,
    }),
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [],
      session_id: session.session_id,
    }),
  });

  store.getState().selectSession(session.session_id);
  store.setState({ pendingHandoff });
  await store.getState().decideHandoff("confirm");

  // 409 handoff_not_pending:清除旧 pending 后 GET 兜底刷新为新值
  assert.equal(store.getState().isDecidingHandoff, false);
  assert.equal(store.getState().pendingHandoff?.interrupt_id, "interrupt-2");
  assert.equal(store.getState().requestError, null);
});

test("decideHandoff surfaces session_busy with a friendly message", async () => {
  const { ApiClientError } = await loadApiClient();
  const { createChatStore } = await loadChatStore();
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    decideHandoff: async () => {
      throw new ApiClientError(
        "Another request is already running for this session.",
        { code: "session_busy", status: null },
      );
    },
    getPendingHandoff: async () => ({
      pending_handoff: null,
      session_id: session.session_id,
    }),
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [],
      session_id: session.session_id,
    }),
  });

  store.getState().selectSession(session.session_id);
  store.setState({ pendingHandoff });
  await store.getState().decideHandoff("confirm");

  assert.equal(store.getState().isDecidingHandoff, false);
  assert.match(store.getState().requestError?.message ?? "", /稍后重试/);
});

test("decideHandoff without a pending handoff skips the client", async () => {
  const { createChatStore } = await loadChatStore();
  let decided = false;
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    decideHandoff: async () => {
      decided = true;
      return { events: [], session_id: session.session_id };
    },
    getPendingHandoff: async () => ({
      pending_handoff: null,
      session_id: session.session_id,
    }),
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [],
      session_id: session.session_id,
    }),
  });

  store.getState().selectSession(session.session_id);
  await store.getState().decideHandoff("confirm");

  assert.equal(decided, false);
  assert.equal(store.getState().isDecidingHandoff, false);
  assert.equal(store.getState().requestError, null);
});

test("decideHandoff surfaces a session_busy run_error embedded in a 200 response", async () => {
  // 后端真实路径:会话忙时 decide_handoff 返回 200 + ChatResponse,
  // 忙态表达在 run_error(error_code=session_busy),不是 HTTP 错误
  // (review 补充测试)。
  const { createChatStore } = await loadChatStore();
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    getPendingHandoff: async () => ({ pending_handoff: null, session_id: session.session_id }),
    listSessions: async () => [session],
    decideHandoff: async () => ({
      events: [],
      message: null,
      pending_handoff: null,
      run_error: {
        agent: null,
        error_code: "session_busy",
        message: "A request is already running for this session.",
      },
      session_id: session.session_id,
    }),
  });

  await store.getState().refreshSessions();
  store.getState().selectSession(session.session_id);
  // 先放入一个 pending(decideHandoff 依赖)
  store.setState({ pendingHandoff: { interrupt_id: "interrupt-1", request: { target_agent: "teaching_assistant", task_content: "检查" } } });
  await store.getState().decideHandoff("confirm");

  const state = store.getState();
  assert.equal(state.isDecidingHandoff, false);
  // 忙态转友好文案(卡片显示「会话正忙,请稍后重试。」)
  assert.equal(state.requestError?.message, "会话正忙,请稍后重试。");
});

test("decideHandoff with modify forwards the modification fields", async () => {
  // D2-T4:modify 的 camelCase 修改字段(targetAgent/taskContent)应转为
  // 契约 snake_case(target_agent/task_content)组装进请求体透传给 client,
  // 与后端双分支校验(仅 modify 携带修改字段)对齐。
  const { createChatStore } = await loadChatStore();
  const decided: Array<{
    action: string;
    interrupt_id: string;
    target_agent?: string | null;
    task_content?: string | null;
  }> = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    decideHandoff: async (_sessionId, decision) => {
      decided.push(decision);
      return {
        events: [],
        message: assistantMessage,
        pending_handoff: null,
        session_id: session.session_id,
      };
    },
    getPendingHandoff: async () => ({
      pending_handoff: null,
      session_id: session.session_id,
    }),
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => ({
      events: [],
      message: assistantMessage,
      session_id: session.session_id,
    }),
  });

  store.getState().selectSession(session.session_id);
  store.setState({ pendingHandoff });
  await store.getState().decideHandoff("modify", {
    targetAgent: "learning_assistant",
    taskContent: "修改后的任务内容。",
  });

  assert.deepEqual(decided, [
    {
      action: "modify",
      interrupt_id: "interrupt-1",
      target_agent: "learning_assistant",
      task_content: "修改后的任务内容。",
    },
  ]);
  assert.equal(store.getState().isDecidingHandoff, false);
  assert.equal(store.getState().requestError, null);
});

// D2-T5:错误降级 UX——lastSentMessage 记录与 retryLastMessage 重发 ——————
test("sendMessage records lastSentMessage and retryLastMessage resends it", async () => {
  const { ApiClientError } = await loadApiClient();
  const { createChatStore } = await loadChatStore();
  const sent: string[] = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async ({ message }) => {
      sent.push(message);
      // 第一次失败(网络错误),第二次成功——模拟「失败后重试」
      if (sent.length === 1) {
        throw new ApiClientError("网络失败。", { code: null, status: null });
      }
      return { events: [], message: assistantMessage, session_id: session.session_id };
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().sendMessage("请重发这条");

  // 发起前已记录,失败后仍保留,便于重试入口使用
  assert.deepEqual(sent, ["请重发这条"]);
  assert.equal(store.getState().lastSentMessage, "请重发这条");
  assert.ok(store.getState().requestError);

  // 未注入流式通道 → retryLastMessage 走 sendMessage(同步通道)重发同一条
  await store.getState().retryLastMessage();
  assert.deepEqual(sent, ["请重发这条", "请重发这条"]);
  assert.equal(store.getState().requestError, null);
});

test("retryLastMessage without a recorded message does nothing", async () => {
  const { createChatStore } = await loadChatStore();
  let sendCount = 0;
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => {
      sendCount += 1;
      return { events: [], session_id: session.session_id };
    },
  });

  // 未发送过任何消息(也无会话):lastSentMessage 为 null,重试为空操作
  await store.getState().retryLastMessage();
  assert.equal(sendCount, 0);
  assert.equal(store.getState().lastSentMessage, null);
});

test("streamSendMessage also records lastSentMessage", async () => {
  const { createChatStore } = await loadChatStore();
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => ({ events: [], session_id: session.session_id }),
    streamChat: async ({ onEvent }) => {
      onEvent({ event_type: "message_end", message: assistantMessage, sequence: 1 });
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().streamSendMessage("流式消息");

  assert.equal(store.getState().lastSentMessage, "流式消息");
  assert.equal(store.getState().isStreaming, false);
});

// D3-T4:回答引用(references)的保存与轮次清空 ————————————————————
test("sendMessage saves response references and resets them when absent", async () => {
  const { createChatStore } = await loadChatStore();
  // 与后端契约字段一致(document_id / source / page / chunk_id)
  const citedAnswer = {
    chunk_id: "ml-zhouzhihua:88:0:500",
    document_id: "ml-zhouzhihua",
    page: 88,
    source: "ml-zhouzhihua",
  };
  let sendCount = 0;
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => {
      sendCount += 1;
      return {
        events: [],
        message: assistantMessage,
        // 第一轮带引用,第二轮不带(undefined 等价于契约缺失)
        references: sendCount === 1 ? [citedAnswer] : undefined,
        session_id: session.session_id,
      };
    },
  });

  // 初始无引用(与 emptyConversationState 一致)
  assert.equal(store.getState().references, null);

  store.getState().selectSession(session.session_id);
  await store.getState().sendMessage("带引用的提问");
  assert.deepEqual(store.getState().references, [citedAnswer]);

  // 第二轮响应无 references → 归一为 null,不残留上一轮引用
  await store.getState().sendMessage("无引用的提问");
  assert.equal(store.getState().references, null);
});
