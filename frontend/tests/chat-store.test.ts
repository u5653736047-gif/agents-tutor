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
  assert.equal(state.requestError, null);
});

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
