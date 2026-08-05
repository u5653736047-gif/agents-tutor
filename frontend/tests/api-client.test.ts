import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const apiClientPath = new URL("../lib/api-client.ts", import.meta.url);

async function loadApiClient() {
  assert.ok(existsSync(apiClientPath), "missing generated-type API client");
  return import("../lib/api-client");
}

test("the API client injects the demo user header and generated session query", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example/",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json([
        {
          archived: false,
          created_at: "2026-08-03T00:00:00Z",
          session_id: "session-1",
          user_id: "demo-user",
        },
      ]);
    },
  });

  const sessions = await client.listSessions(true);

  assert.equal(sessions[0]?.session_id, "session-1");
  assert.equal(requests[0]?.url, "https://api.example/sessions?include_archived=true");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
});

test("the API client preserves an optional generated session ID", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ body: string | null }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (_input, init) => {
      requests.push({ body: String(init?.body ?? null) });
      return Response.json({
        archived: false,
        created_at: "2026-08-03T00:00:00Z",
        session_id: "session/1",
        user_id: "demo-user",
      });
    },
  });

  await client.createSession({ session_id: "session/1" });

  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), { session_id: "session/1" });
});

test("the API client exposes non-success responses as one error shape", async () => {
  const { ApiClientError, createApiClient } = await loadApiClient();
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          detail: { error_code: "session_not_found", message: "会话不存在。" },
        }),
        { headers: { "Content-Type": "application/json" }, status: 404 },
      ),
  });

  await assert.rejects(client.getSessionMessages("missing"), (error: unknown) => {
    assert.ok(error instanceof ApiClientError);
    assert.equal(error.status, 404);
    assert.equal(error.code, "session_not_found");
    assert.equal(error.message, "会话不存在。");
    return true;
  });
});

test("the API client reports an aborted request through the same error shape", async () => {
  const { ApiClientError, createApiClient } = await loadApiClient();
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Request aborted", "AbortError")),
          { once: true },
        );
      }),
    timeoutMs: 1,
  });

  await assert.rejects(client.listSessions(), (error: unknown) => {
    assert.ok(error instanceof ApiClientError);
    assert.equal(error.status, null);
    assert.equal(error.code, null);
    return true;
  });
});

test("the handoff client only serializes supported decision fields", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ body: string | null; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ body: String(init?.body ?? null), url: String(input) });
      return Response.json({ events: [], session_id: "session/1" });
    },
  });

  await client.decideHandoff("session/1", {
    action: "confirm",
    interrupt_id: "interrupt-1",
  });

  assert.equal(requests[0]?.url, "https://api.example/sessions/session%2F1/handoff");
  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), {
    action: "confirm",
    interrupt_id: "interrupt-1",
  });
});

// D6-T2:反馈提交 ———————————————————————————————————————————————
test("submitFeedback posts snake_case fields with the user header", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{
    body: string | null;
    init: RequestInit | undefined;
    url: string;
  }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ body: String(init?.body ?? null), init, url: String(input) });
      return Response.json({ received: true });
    },
  });

  const result = await client.submitFeedback({
    sessionId: "session/1",
    messageId: "2026-08-03T00:00:00Z",
    rating: "down",
    comment: "回答有误",
    errorCode: "model_call_failed",
  });

  assert.equal(requests[0]?.url, "https://api.example/feedback");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), {
    session_id: "session/1",
    message_id: "2026-08-03T00:00:00Z",
    rating: "down",
    comment: "回答有误",
    error_code: "model_call_failed",
  });
  assert.equal(result.received, true);
});

test("submitFeedback omits optional fields and normalizes the response", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ body: string | null }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (_input, init) => {
      requests.push({ body: String(init?.body ?? null) });
      return Response.json({});
    },
  });

  const result = await client.submitFeedback({ sessionId: "s1", rating: "up" });

  // 未传的可选字段不落键(契约可空);空文本 comment 仍会带上(点踩
  // 纠错空文本也允许提交)
  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), {
    session_id: "s1",
    rating: "up",
  });
  // 契约 received 默认 true 但可能缺省,归一为严格布尔语义
  assert.equal(result.received, false);
});

// D6-T4:知识库检索 ———————————————————————————————————————————————
test("searchKnowledge posts snake_case top_k with the user header and passes hits through", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{
    body: string | null;
    init: RequestInit | undefined;
    url: string;
  }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ body: String(init?.body ?? null), init, url: String(input) });
      return Response.json({
        hits: [
          {
            summary: "反向传播通过链式法则计算梯度。",
            citation: {
              chunk_id: "chunk-1",
              document_id: "doc-1",
              page: 3,
              source: "ml-notes.pdf",
            },
            score: 0.87654,
          },
        ],
      });
    },
  });

  const result = await client.searchKnowledge({ query: "反向传播", topK: 3 });

  assert.equal(requests[0]?.url, "https://api.example/knowledge/search");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  // 调用侧 camelCase topK → 契约 snake_case top_k
  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), {
    query: "反向传播",
    top_k: 3,
  });
  // 响应 { hits } 直接透传,契约 snake_case 字段原样(与 Session 等先例一致)
  assert.equal(result.hits[0]?.summary, "反向传播通过链式法则计算梯度。");
  assert.equal(result.hits[0]?.citation.document_id, "doc-1");
  assert.equal(result.hits[0]?.citation.page, 3);
  assert.equal(result.hits[0]?.score, 0.87654);
});

test("searchKnowledge defaults top_k to 5 and normalizes knowledge_unavailable errors", async () => {
  const { ApiClientError, createApiClient } = await loadApiClient();
  const requests: Array<{ body: string | null }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (_input, init) => {
      requests.push({ body: String(init?.body ?? null) });
      return new Response(
        JSON.stringify({
          detail: { error_code: "knowledge_unavailable", message: "知识库未就绪。" },
        }),
        { headers: { "Content-Type": "application/json" }, status: 503 },
      );
    },
  });

  await assert.rejects(client.searchKnowledge({ query: "测试" }), (error: unknown) => {
    assert.ok(error instanceof ApiClientError);
    assert.equal(error.status, 503);
    assert.equal(error.code, "knowledge_unavailable");
    assert.equal(error.message, "知识库未就绪。");
    return true;
  });
  // topK 未传 → 落契约默认 top_k: 5
  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), { query: "测试", top_k: 5 });
});
