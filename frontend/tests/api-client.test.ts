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

  await client.createSession({
    session_id: "session/1",
    workspace_root: "D:\\Projects\\course",
  });

  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), {
    session_id: "session/1",
    workspace_root: "D:\\Projects\\course",
  });
});

test("the API client validates, browses, and adds workspace directories", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ body: string | null; method: string; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({
        body: init?.body ? String(init.body) : null,
        method: init?.method ?? "GET",
        url: String(input),
      });
      if (String(input).includes("/directories")) {
        return Response.json({
          directories: [],
          parent: "D:\\Projects",
          path: "D:\\Projects\\course",
        });
      }
      if (String(input).includes("/validate")) {
        return Response.json({ name: "course", path: "D:\\Projects\\course" });
      }
      return Response.json({
        additional_workspace_roots: ["D:\\Shared"],
        archived: false,
        created_at: "2026-08-03T00:00:00Z",
        session_id: "session/1",
        updated_at: "2026-08-03T00:00:00Z",
        user_id: "demo-user",
        workspace_access: "read_only",
        workspace_root: "D:\\Projects\\course",
      });
    },
  });

  await client.validateWorkspace("D:\\Projects\\course");
  await client.listWorkspaceDirectories("D:\\Projects\\course");
  await client.addWorkspaceRoot("session/1", "D:\\Shared");

  assert.equal(requests[0]?.url, "https://api.example/workspaces/validate");
  assert.deepEqual(JSON.parse(requests[0]?.body ?? "{}"), {
    path: "D:\\Projects\\course",
  });
  assert.equal(
    requests[1]?.url,
    "https://api.example/workspaces/directories?path=D%3A%5CProjects%5Ccourse",
  );
  assert.equal(requests[2]?.url, "https://api.example/sessions/session%2F1/workspace-roots");
  assert.deepEqual(JSON.parse(requests[2]?.body ?? "{}"), { path: "D:\\Shared" });
});

test("the API client loads a replayable session process snapshot", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: string[] = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input) => {
      requests.push(String(input));
      return Response.json({
        current_agent: "learning_assistant",
        events: [
          {
            agent: "learning_assistant",
            content: "先检索再解释",
            event_type: "reasoning",
            message_id: "assistant-step-1",
            sequence: 2,
            session_id: "session/1",
          },
        ],
        task_plan: null,
        task_results: null,
      });
    },
  });

  const process = await client.getSessionProcess("session/1");

  assert.equal(requests[0], "https://api.example/sessions/session%2F1/process");
  assert.equal(process.events[0]?.event_type, "reasoning");
  assert.equal(process.current_agent, "learning_assistant");
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

test("the tool approval client keeps the exact interrupt and encoded session path", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ body: string | null; method?: string; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({
        body: init?.body == null ? null : String(init.body),
        method: init?.method,
        url: String(input),
      });
      return Response.json({ events: [], session_id: "session/1" });
    },
  });

  await client.decideToolApproval("session/1", {
    action: "reject",
    interrupt_id: "interrupt-shell-1",
  });

  assert.deepEqual(requests[0], {
    body: JSON.stringify({
      action: "reject",
      interrupt_id: "interrupt-shell-1",
    }),
    method: "POST",
    url: "https://api.example/sessions/session%2F1/tool-approval",
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

// D6-T6:知识库文档管理 ———————————————————————————————————————————
test("uploadDocument posts FormData under the file field and passes the parse receipt through", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json(
        {
          chunk_count: 42,
          document_id: "doc-1",
          page_count: 8,
          source: "ml-notes.pdf",
        },
        { status: 201 },
      );
    },
  });

  const file = new File(["hello"], "ml-notes.pdf", { type: "application/pdf" });
  const progress: number[] = [];
  const uploaded = await client.uploadDocument(file, (fraction) => progress.push(fraction));

  assert.equal(requests[0]?.url, "https://api.example/knowledge/documents");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  // body 为 FormData、字段名 "file";Content-Type 不客户端强设(浏览器
  // 自动带 multipart boundary;强设 application/json 会破坏分界)
  assert.ok(requests[0]?.init?.body instanceof FormData);
  assert.equal((requests[0]?.init?.body as FormData).get("file"), file);
  assert.equal(new Headers(requests[0]?.init?.headers).has("Content-Type"), false);
  // 响应透传契约字段;进度回调 0 → 1(fetch 无上传进度,仅里程碑)
  assert.equal(uploaded.document_id, "doc-1");
  assert.equal(uploaded.source, "ml-notes.pdf");
  assert.equal(uploaded.page_count, 8);
  assert.equal(uploaded.chunk_count, 42);
  assert.deepEqual(progress, [0, 1]);
});

test("uploadDocument resets the progress milestone when the request fails", async () => {
  const { ApiClientError, createApiClient } = await loadApiClient();
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          detail: { error_code: "invalid_request", message: "文件类型或大小不符。" },
        }),
        { headers: { "Content-Type": "application/json" }, status: 422 },
      ),
  });

  const progress: number[] = [];
  await assert.rejects(
    client.uploadDocument(new File(["x"], "bad.exe"), (fraction) => progress.push(fraction)),
    (error: unknown) => {
      assert.ok(error instanceof ApiClientError);
      assert.equal(error.status, 422);
      assert.equal(error.code, "invalid_request");
      return true;
    },
  );
  // 失败复位为 0,调用方以「上传中」状态自行收尾
  assert.deepEqual(progress, [0, 0]);
});

test("listDocuments GETs /knowledge/documents and passes documents through", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json({
        documents: [
          {
            chunk_count: 42,
            document_id: "doc-1",
            page_count: 8,
            source: "ml-notes.pdf",
          },
        ],
      });
    },
  });

  const response = await client.listDocuments();

  assert.equal(requests[0]?.url, "https://api.example/knowledge/documents");
  // GET 不显式设 method(浏览器默认),仍带用户头
  assert.equal(requests[0]?.init?.method, undefined);
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  assert.equal(response.documents[0]?.document_id, "doc-1");
  assert.equal(response.documents[0]?.source, "ml-notes.pdf");
  assert.equal(response.documents[0]?.page_count, 8);
  assert.equal(response.documents[0]?.chunk_count, 42);
});

test("deleteDocument DELETEs /knowledge/documents/{id} and tolerates a 204 empty body", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return new Response(null, { status: 204 });
    },
  });

  await client.deleteDocument("doc/1");

  assert.equal(requests[0]?.url, "https://api.example/knowledge/documents/doc%2F1");
  assert.equal(requests[0]?.init?.method, "DELETE");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
});

// D6-T7:学习进度统计 ———————————————————————————————————————————————
test("getStatsOverview GETs /stats/overview with the user header and passes the contract through", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json({
        agent_answer_counts: {
          evaluator: 1,
          learning_assistant: 2,
          supervisor: 3,
          teaching_assistant: 1,
        },
        last_activity_at: "2026-08-03T10:00:00+00:00",
        message_count: 7,
        session_count: 2,
      });
    },
  });

  const overview = await client.getStatsOverview();

  assert.equal(requests[0]?.url, "https://api.example/stats/overview");
  // GET 不显式设 method(浏览器默认),仍带用户头
  assert.equal(requests[0]?.init?.method, undefined);
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  // 响应契约 snake_case 字段原样透传(与 Session 等先例一致)
  assert.equal(overview.session_count, 2);
  assert.equal(overview.message_count, 7);
  assert.deepEqual(overview.agent_answer_counts, {
    evaluator: 1,
    learning_assistant: 2,
    supervisor: 3,
    teaching_assistant: 1,
  });
  assert.equal(overview.last_activity_at, "2026-08-03T10:00:00+00:00");
});

// 赛前可视化增强:学情诊断与洞察客户端 —————————————————————————
test("getDiagnosisSummary GETs /learning/diagnosis/summary with the user header", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json({
        knowledge_points: [
          {
            accuracy: 0.333,
            attempts: 3,
            correct: 1,
            knowledge_point: "梯度下降",
            last_at: "2026-08-30T08:00:00+00:00",
          },
        ],
        total_attempts: 5,
        uncategorized_attempts: 0,
        user_id: "demo-user",
        weak_points: ["梯度下降"],
      });
    },
  });

  const summary = await client.getDiagnosisSummary();

  assert.equal(requests[0]?.url, "https://api.example/learning/diagnosis/summary");
  assert.equal(requests[0]?.init?.method, undefined);
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  assert.equal(summary.total_attempts, 5);
  assert.deepEqual(summary.weak_points, ["梯度下降"]);
});

test("getLearningInsights GETs /learning/insights/summary and passes the contract through", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json({
        daily_accuracy: [{ accuracy: 0.75, attempts: 4, date: "2026-08-30" }],
        error_tag_counts: { "概念不清": 2, "计算失误": 1 },
        recent_path_plans: [
          { created_at: "2026-08-30T09:00:00+00:00", knowledge_point: "链式法则" },
        ],
        total_wrong: 3,
        user_id: "demo-user",
      });
    },
  });

  const insights = await client.getLearningInsights();

  assert.equal(requests[0]?.url, "https://api.example/learning/insights/summary");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  assert.equal(insights.total_wrong, 3);
  assert.deepEqual(insights.error_tag_counts, { "概念不清": 2, "计算失误": 1 });
  assert.equal(insights.daily_accuracy[0]?.accuracy, 0.75);
  assert.equal(insights.recent_path_plans[0]?.knowledge_point, "链式法则");
});

test("getStatsOverview normalizes stats errors like other endpoints", async () => {
  const { ApiClientError, createApiClient } = await loadApiClient();
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          detail: { error_code: "internal_error", message: "The request could not be completed." },
        }),
        { headers: { "Content-Type": "application/json" }, status: 500 },
      ),
  });

  await assert.rejects(client.getStatsOverview(), (error: unknown) => {
    assert.ok(error instanceof ApiClientError);
    assert.equal(error.status, 500);
    assert.equal(error.code, "internal_error");
    return true;
  });
});

// D7-T2:聊天附件 ———————————————————————————————————————————————
test("uploadFile posts FormData under the file field and passes the receipt through", async () => {
  const { createApiClient } = await loadApiClient();
  const requests: Array<{ init: RequestInit | undefined; url: string }> = [];
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async (input, init) => {
      requests.push({ init, url: String(input) });
      return Response.json(
        {
          content_type: "image/png",
          file_id: "abc123.png",
          name: "diagram.png",
          size: 1024,
          url: "/files/abc123.png",
        },
        { status: 201 },
      );
    },
  });

  const file = new File(["png"], "diagram.png", { type: "image/png" });
  const receipt = await client.uploadFile(file);

  assert.equal(requests[0]?.url, "https://api.example/files");
  assert.equal(requests[0]?.init?.method, "POST");
  assert.equal(new Headers(requests[0]?.init?.headers).get("X-User-Id"), "demo-user");
  // body 为 FormData、字段名 "file";Content-Type 不客户端强设
  // (浏览器自动带 multipart boundary,与 uploadDocument 同通道)
  assert.ok(requests[0]?.init?.body instanceof FormData);
  assert.equal((requests[0]?.init?.body as FormData).get("file"), file);
  assert.equal(new Headers(requests[0]?.init?.headers).has("Content-Type"), false);
  // 响应透传契约字段(file_id/name/content_type/size/url,单字段名原样)
  assert.equal(receipt.file_id, "abc123.png");
  assert.equal(receipt.name, "diagram.png");
  assert.equal(receipt.content_type, "image/png");
  assert.equal(receipt.size, 1024);
  assert.equal(receipt.url, "/files/abc123.png");
});

test("uploadFile normalizes upload failures like other endpoints", async () => {
  const { ApiClientError, createApiClient } = await loadApiClient();
  const client = createApiClient({
    baseUrl: "https://api.example",
    fetchImpl: async () =>
      new Response(
        JSON.stringify({
          detail: { error_code: "invalid_request", message: "文件类型或大小不符。" },
        }),
        { headers: { "Content-Type": "application/json" }, status: 422 },
      ),
  });

  await assert.rejects(client.uploadFile(new File(["x"], "bad.exe")), (error: unknown) => {
    assert.ok(error instanceof ApiClientError);
    assert.equal(error.status, 422);
    assert.equal(error.code, "invalid_request");
    assert.equal(error.message, "文件类型或大小不符。");
    return true;
  });
});

test("getFileUrl builds the controlled download URL with an encoded file id", async () => {
  const { createApiClient } = await loadApiClient();
  const client = createApiClient({ baseUrl: "https://api.example/" });

  // 注入 base 拼接(尾斜杠剥除);file_id 特殊字符经 encodeURIComponent 兜底
  assert.equal(client.getFileUrl("abc123.png"), "https://api.example/files/abc123.png");
  assert.equal(client.getFileUrl("a/b.png"), "https://api.example/files/a%2Fb.png");
});

test("the module-level getFileUrl proxies the default client", async () => {
  const { apiBaseUrl, getFileUrl } = await loadApiClient();

  assert.equal(getFileUrl("abc123.png"), `${apiBaseUrl}/files/abc123.png`);
});
