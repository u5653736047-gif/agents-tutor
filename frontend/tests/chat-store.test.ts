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

test("switching away and back restores the persisted collaboration process", async () => {
  const { createChatStore } = await loadChatStore();
  const replayedEvent = {
    agent: "learning_assistant" as const,
    content: "先识别链式法则，再说明梯度回传",
    event_type: "reasoning" as const,
    message_id: "assistant-step-1",
    sequence: 2,
    session_id: "session-1",
  };
  const toolEvent = {
    agent: "learning_assistant" as const,
    event_type: "tool_call" as const,
    input_summary: '{"query":"反向传播"}',
    sequence: 3,
    session_id: "session-1",
    tool_call_id: "call-search-1",
    tool_name: "search_knowledge",
  };
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async (sessionId) =>
      sessionId === "session-1" ? [userMessage, assistantMessage] : [],
    getSessionProcess: async (sessionId) => ({
      current_agent: sessionId === "session-1" ? "learning_assistant" : null,
      events: sessionId === "session-1" ? [replayedEvent, toolEvent] : [],
      task_plan: null,
      task_results: null,
    }),
    listSessions: async () => [session],
    sendChat: async () => ({ events: [], session_id: session.session_id }),
  });

  store.getState().selectSession("session-1");
  await store.getState().loadCurrentSessionMessages();
  assert.deepEqual(store.getState().events, [replayedEvent, toolEvent]);

  store.getState().selectSession("session-2");
  await store.getState().loadCurrentSessionMessages();
  assert.deepEqual(store.getState().events, []);

  store.getState().selectSession("session-1");
  await store.getState().loadCurrentSessionMessages();
  assert.deepEqual(store.getState().events, [replayedEvent, toolEvent]);
  assert.equal(store.getState().currentAgent, "learning_assistant");
});

// D4-T2:乐观更新与失败回滚 ——————————————————————————————
test("sendMessage optimistically shows the user message then replaces with the authoritative history", async () => {
  const { createChatStore } = await loadChatStore();
  let resolveChat: ((value: { events: []; session_id: string }) => void) | undefined;
  let markChatStarted: (() => void) | undefined;
  const chatStarted = new Promise<void>((resolve) => {
    markChatStarted = resolve;
  });
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage, assistantMessage],
    listSessions: async () => [session],
    sendChat: async () => {
      markChatStarted?.();
      return new Promise((resolve) => {
        resolveChat = resolve;
      });
    },
  });

  store.getState().selectSession(session.session_id);
  const sending = store.getState().sendMessage("请解释概念");
  await chatStarted;

  // 响应未返回:用户消息已乐观显示(created_at 为本地占位 undefined,
  // 与后端历史消息的形状差异仅在此占位键)
  assert.equal(store.getState().isSending, true);
  assert.deepEqual(store.getState().messages, [
    { agent: null, content: "请解释概念", created_at: undefined, role: "user" },
  ]);

  resolveChat?.({ events: [], session_id: session.session_id });
  await sending;

  // 成功:权威历史整体替换乐观消息(用户消息在后端历史中天然存在)
  assert.deepEqual(store.getState().messages, [userMessage, assistantMessage]);
  assert.equal(store.getState().isSending, false);
  assert.equal(store.getState().requestError, null);
});

test("sendMessage rolls back the optimistic message on failure", async () => {
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

  // 失败后乐观消息被回滚:列表恢复为空,错误与状态复位
  const state = store.getState();
  assert.deepEqual(state.messages, []);
  assert.deepEqual(state.events, []);
  assert.equal(state.isSending, false);
  assert.equal(state.requestError, failure);
});

test("sendMessage rollback removes only the optimistic message", async () => {
  const { ApiClientError } = await loadApiClient();
  const { createChatStore } = await loadChatStore();
  const failure = new ApiClientError("网络失败。", { code: null, status: null });
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
  // 历史中已存在一条同内容同 role 的用户消息(权威历史,非本次追加)
  store.setState({ messages: [userMessage] });
  await store.getState().sendMessage("请解释概念");

  // 按对象引用回滚:只移除本次追加的乐观消息,历史同内容消息保留
  assert.deepEqual(store.getState().messages, [userMessage]);
  assert.equal(store.getState().isSending, false);
  assert.equal(store.getState().requestError, failure);
});

test("sendMessage keeps optimistic messages in order for two consecutive sends", async () => {
  const { createChatStore } = await loadChatStore();
  const resolvers: Array<(value: { events: []; session_id: string }) => void> = [];
  let started = 0;
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => {
      started += 1;
      return new Promise((resolve) => {
        resolvers.push(resolve);
      });
    },
  });

  store.getState().selectSession(session.session_id);
  // 两次发送均未完成:乐观消息按调用顺序追加(函数式 set 各自基于
  // 最新 state,不互相覆盖)
  const first = store.getState().sendMessage("第一条");
  const second = store.getState().sendMessage("第二条");

  assert.equal(started, 2);
  assert.deepEqual(
    store.getState().messages.map((item) => item.content),
    ["第一条", "第二条"],
  );

  // 依次放行:权威历史(空)整体替换两条乐观消息
  resolvers[0]?.({ events: [], session_id: session.session_id });
  resolvers[1]?.({ events: [], session_id: session.session_id });
  await Promise.all([first, second]);
  assert.deepEqual(store.getState().messages, []);
  assert.equal(store.getState().isSending, false);
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

// D4-T7:归档视图(showArchived)与归档后刷新 ————————————————————
test("setShowArchived toggles the flag and refetches with the archive flag", async () => {
  const { createChatStore } = await loadChatStore();
  const calls: Array<boolean | undefined> = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async (includeArchived) => {
      calls.push(includeArchived);
      return [session];
    },
    sendChat: async () => ({ events: [], session_id: session.session_id }),
  });

  // 初始未归档:refreshSessions 显式传 false(= 只取未归档,与不传等价)
  await store.getState().refreshSessions();
  assert.deepEqual(calls, [false]);
  assert.equal(store.getState().showArchived, false);

  // 切换归档:状态更新并触发重新拉取(带 include_archived=true)
  store.getState().setShowArchived(true);
  // setShowArchived 内部以 void 触发异步 refreshSessions,等微任务落定
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, [false, true]);
  assert.equal(store.getState().showArchived, true);
  assert.deepEqual(store.getState().sessions, [session]);

  // 切回未归档:同样按新状态重新拉取
  store.getState().setShowArchived(false);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(calls, [false, true, false]);
  assert.equal(store.getState().showArchived, false);
});

test("archiveSession refreshes the session list after success", async () => {
  const { createChatStore } = await loadChatStore();
  let listCalls = 0;
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => {
      listCalls += 1;
      return [session];
    },
    sendChat: async () => ({ events: [], session_id: session.session_id }),
  });

  await store.getState().refreshSessions();
  assert.equal(listCalls, 1);

  store.getState().selectSession(session.session_id);
  await store.getState().archiveSession(session.session_id);
  // 归档成功:本地乐观移除 + 触发列表刷新(与服务端对齐)
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(listCalls, 2);
  assert.equal(store.getState().currentSessionId, null);
  assert.equal(store.getState().requestError, null);
});

// D6-T2:submitFeedback 只转发、不写 requestError、失败原样抛 —————
test("submitFeedback forwards to the client without touching requestError", async () => {
  const { createChatStore } = await loadChatStore();
  const calls: Array<{ rating: string; comment?: string }> = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => ({ events: [], session_id: session.session_id }),
    submitFeedback: async (input) => {
      // 归一:忽略未提供的可选键,便于精确断言
      calls.push({
        rating: input.rating,
        ...(input.comment ? { comment: input.comment } : {}),
      });
    },
  });

  await store.getState().submitFeedback({ rating: "up" });
  assert.deepEqual(calls, [{ rating: "up" }]);
  // 反馈独立于主流程:成功不产生 requestError
  assert.equal(store.getState().requestError, null);

  await store.getState().submitFeedback({ rating: "down", comment: "这段回答有误" });
  assert.deepEqual(calls, [
    { rating: "up" },
    { rating: "down", comment: "这段回答有误" },
  ]);
  assert.equal(store.getState().requestError, null);
});

test("submitFeedback rethrows client failures for the component to surface", async () => {
  const { createChatStore } = await loadChatStore();
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [],
    listSessions: async () => [session],
    sendChat: async () => ({ events: [], session_id: session.session_id }),
    submitFeedback: async () => {
      throw new Error("storage full");
    },
  });

  // 失败原样抛给调用方(feedback-buttons 组件 catch 显示错误行),
  // 且不写入全局 requestError(静默降级,不阻塞对话)
  await assert.rejects(
    () => store.getState().submitFeedback({ rating: "up" }),
    /storage full/,
  );
  assert.equal(store.getState().requestError, null);
});

// D7-T2:附件透传 ———————————————————————————————————————————————
test("sendMessage forwards attachments into the ChatRequest body", async () => {
  const { createChatStore } = await loadChatStore();
  const bodies: Array<Record<string, unknown>> = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage],
    listSessions: async () => [session],
    sendChat: async (payload) => {
      bodies.push(payload as unknown as Record<string, unknown>);
      return { events: [], session_id: session.session_id };
    },
  });

  store.getState().selectSession(session.session_id);
  const attachments = [
    { content_type: "image/png", file_id: "abc123.png", name: "diagram.png", size: 1024 },
  ];
  await store.getState().sendMessage("看图", attachments);

  // 附件回执按契约 ChatRequest.attachments 原样透传进 body
  assert.deepEqual(bodies[0], {
    attachments,
    message: "看图",
    session_id: session.session_id,
  });
});

test("sendMessage omits the attachments key when none are provided", async () => {
  // 向后兼容:不传附件时 body 与既有行为逐字节一致(不落 attachments 键)
  const { createChatStore } = await loadChatStore();
  const bodies: Array<Record<string, unknown>> = [];
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage],
    listSessions: async () => [session],
    sendChat: async (payload) => {
      bodies.push(payload as unknown as Record<string, unknown>);
      return { events: [], session_id: session.session_id };
    },
  });

  store.getState().selectSession(session.session_id);
  await store.getState().sendMessage("普通消息");

  assert.deepEqual(bodies[0], { message: "普通消息", session_id: session.session_id });
});

test("streamSendMessage carries attachments on the streaming channel", async () => {
  // D7-T2:stream-client 已扩展 attachments 透传(与同步通道同契约),
  // 有附件消息正常走流式主通道(获得流式体验),重试耗尽才降级同步。
  const { createChatStore } = await loadChatStore();
  const bodies: Array<Record<string, unknown>> = [];
  let streamCalls = 0;
  let streamAttachments: unknown = "not-called";
  const store = createChatStore({
    archiveSession: async () => session,
    createSession: async () => session,
    getSessionMessages: async () => [userMessage],
    listSessions: async () => [session],
    sendChat: async (payload) => {
      bodies.push(payload as unknown as Record<string, unknown>);
      return { events: [], session_id: session.session_id };
    },
    streamChatWithRetry: async (options) => {
      streamCalls += 1;
      streamAttachments = options.attachments;
    },
  });

  store.getState().selectSession(session.session_id);
  const attachments = [
    { content_type: "text/plain", file_id: "note.txt", name: "note.txt", size: 42 },
  ];
  await store.getState().streamSendMessage("带附件", attachments);

  // 附件消息:流式通道被调用且收到 attachments,同步通道零调用
  assert.equal(streamCalls, 1);
  assert.deepEqual(streamAttachments, attachments);
  assert.equal(bodies.length, 0);
  assert.equal(store.getState().isStreaming, false);
  assert.equal(store.getState().isSending, false);
});
