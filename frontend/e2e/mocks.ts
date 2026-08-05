// ============================================================================
// D6-T8 E2E mock 层(前端路由拦截)
//
// 策略:在浏览器内用 page.route 拦截全部 API 请求,按前端解析契约
// (lib/api-client.ts 的 request<T>、lib/stream-client.ts 的 SSE 解析)
// 伪造响应,不启动 FastAPI 替身。mock 响应字段名一律以
// contracts/api.generated.ts(单一数据源)为准,只给前端实际读取的字段。
//
// 端点 → 契约对照(api-client.ts 实际调用路径):
//   GET  /healthz                     → { status: "ok" }
//   POST /sessions                    → Session(createSession)
//   GET  /sessions[?include_archived] → Session[](listSessions)
//   GET  /sessions/{id}/messages      → Message[] 数组(直接返回数组,
//                                       没有外层对象——以 api-client 为准)
//   POST /chat                        → ChatResponse(sendChat)
//   POST /chat/stream?from_sequence=N → SSE 帧序列(stream-client 逐行解析)
//   POST /sessions/{id}/handoff       → ChatResponse(decideHandoff)
//   GET  /sessions/{id}/handoff       → PendingHandoffResponse
//   POST /sessions/{id}/archive       → Session(archiveSession 期望 Session,
//                                       不是 204——以 api-client 为准)
//   其它指向假后端(127.0.0.1:9999)的请求 → 404 兜底
//
// SSE 帧序列与 stream-client 解析逐字段对应:
//   stream-client 按行重组:data: <json> 行 + 空行(\n\n)为一帧,
//   帧负载 JSON.parse 后作为 StreamEvent 回调给 chat-store.dispatch:
//     thinking     → streamingAgent(event.agent)
//     tool_call    → events 追加(摘要,仅 tool_name/success,不含参数/正文)
//     tool_result  → events 追加(摘要 + duration_ms)
//     message_end  → streamingMessage(event.message,缺失时用 content 兜底)
//                    + references(event.citations)
//     done         → 流结束标记(streamChatWithRetry 以 sawDone 判定成功,
//                    无 done 视为断线并重连续传)
// ============================================================================

import type { Page, Route } from "@playwright/test";

// 契约形状(与 api.generated.ts 的 Session/Message/PendingHandoff 同构,
// 仅保留前端实际读取的字段)
export type AgentRole =
  | "supervisor"
  | "teaching_assistant"
  | "learning_assistant"
  | "evaluator";
export type WorkerAgentRole = "teaching_assistant" | "learning_assistant" | "evaluator";

export type MockSession = {
  archived: boolean;
  created_at: string;
  session_id: string;
  user_id: string | null;
};

export type MockMessage = {
  agent?: AgentRole | null;
  content: string;
  created_at?: string | null;
  role: "user" | "assistant";
};

export type MockPendingHandoff = {
  interrupt_id: string;
  request: {
    plan_step_sequence?: number | null;
    target_agent: WorkerAgentRole;
    task_content: string;
  };
};

// ── 模块级可变状态 ──────────────────────────────────────────────
// 用例内跨请求保持(建会话 → 列表 → 消息 → 归档);每次 installMocks
// 重置,配合 playwright.config 的 fullyParallel: false 保证用例隔离。
let mockSessions: MockSession[] = [];
const mockMessagesBySession = new Map<string, MockMessage[]>();
let mockPendingHandoff: MockPendingHandoff | null = null;
let failStreaming = false;
let sessionCounter = 0;

export type InstallMocksOptions = {
  // 用例 2:流式通道恒 500 → streamChatWithRetry 重试耗尽 → store 降级
  // 同步 /chat,再由同步响应携带 pending_handoff 呈现审批卡片
  failStreaming?: boolean;
  // 同步 /chat 与 GET /handoff 返回的待审批(用例 2 使用)
  pendingHandoff?: MockPendingHandoff | null;
  // 预置会话与消息(用例 3 历史回溯 / 用例 4 归档)
  seedSessions?: MockSession[];
  seedMessages?: Record<string, MockMessage[]>;
};

export function mockSession(sessionId: string): MockSession {
  return {
    archived: false,
    created_at: new Date().toISOString(),
    session_id: sessionId,
    user_id: "demo-user",
  };
}

// 回答文本 = 固定前缀 + 问题原文:断言「完整回答」时可用全文精确匹配,
// 也顺带验证「回答与提问对应」
export function mockAnswerFor(question: string): string {
  return `E2E 模拟回答:${question}(由 mock 生成,无真实模型调用)`;
}

function iso(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString();
}

// 把一轮问答写入 mock 内存历史(权威历史 = getSessionMessages 的返回,
// 前端 store 在流/同步完成后都会拉全量覆盖)
function appendHistory(
  sessionId: string,
  userContent: string,
  assistantContent: string,
): string {
  const history = mockMessagesBySession.get(sessionId) ?? [];
  const assistantAt = iso(1);
  history.push({ role: "user", content: userContent, created_at: iso() });
  history.push({
    role: "assistant",
    content: assistantContent,
    agent: "supervisor",
    created_at: assistantAt,
  });
  mockMessagesBySession.set(sessionId, history);
  return assistantAt;
}

function jsonHeaders(): Record<string, string> {
  return { "content-type": "application/json" };
}

// SSE 帧:data: <json> 行 + 空行分帧(stream-client 的 parseDataLine/flush)
function sseFrame(payload: unknown): string {
  return `data: ${JSON.stringify(payload)}\n\n`;
}

// 从路径 /sessions/{id}/... 提取会话 id(契约 id 由前端
// encodeURIComponent 编码,这里反转义)
function sessionIdFromPath(route: Route): string {
  const segments = new URL(route.request().url()).pathname
    .split("/")
    .filter(Boolean);
  return decodeURIComponent(segments[1] ?? "");
}

export async function installMocks(
  page: Page,
  options: InstallMocksOptions = {},
): Promise<void> {
  // ── 重置模块状态(每次安装 = 用例级隔离) ──
  mockSessions = [...(options.seedSessions ?? [])];
  mockMessagesBySession.clear();
  for (const [sessionId, messages] of Object.entries(options.seedMessages ?? {})) {
    mockMessagesBySession.set(sessionId, [...messages]);
  }
  mockPendingHandoff = options.pendingHandoff ?? null;
  failStreaming = options.failStreaming ?? false;

  // 注册顺序:先注册最泛的兜底 404,再注册具体路径——Playwright 的
  // route 按「后注册先匹配」(LIFO)处理,具体 handler 后注册才优先
  // (此前兜底最后注册导致所有请求被 404,review 实测修正)。
  await page.route(/^http:\/\/127\.0\.0\.1:9999\//, (route) =>
    route.fulfill({
      status: 404,
      headers: jsonHeaders(),
      body: JSON.stringify({
        detail: {
          error_code: "not_found",
          message: "mock: unmatched request",
        },
      }),
    }),
  );

  // ── GET /healthz ──
  // 首页 /healthz 由 Next 服务端 fetch(page.route 拦不到,见
  // playwright.config.ts 注释),此处为防御性兜底(若未来改客户端探测)。
  await page.route("**/healthz", (route) =>
    route.fulfill({
      status: 200,
      headers: jsonHeaders(),
      body: JSON.stringify({ status: "ok" }),
    }),
  );

  // ── POST /chat/stream?from_sequence=N(SSE) ──
  await page.route("**/chat/stream", async (route) => {
    const fromSequence =
      new URL(route.request().url()).searchParams.get("from_sequence") ?? "0";
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      message?: string;
      session_id?: string;
    };
    const sessionId = body.session_id ?? "mock-session";
    const question = body.message ?? "";

    if (failStreaming) {
      // 用例 2:恒 500(ErrorResponse 形状,stream-client readErrorDetail
      // 按 detail.error_code / detail.message 解析)→ 重试耗尽 → 降级同步
      await route.fulfill({
        status: 500,
        headers: jsonHeaders(),
        body: JSON.stringify({
          detail: { error_code: "internal_error", message: "mock 流式通道失败" },
        }),
      });
      return;
    }

    if (fromSequence !== "0") {
      // 断线续传(D1-T3):首响应未发 done 触发重连,这里只补 done 收尾
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: sseFrame({ event_type: "done", sequence: 5, session_id: sessionId }),
      });
      return;
    }

    // 新 run:写入 mock 历史并返回首段 SSE。
    // 首响应刻意止于 message_end、不发 done:streamChatWithRetry 把
    // 「流关闭但未收到 done」视为断线并退避重试续传(约 1s 窗口)——
    // 期间 streaming-message 气泡持续可见,E2E 可稳定断言「流式气泡
    // 出现」,同时顺带覆盖 D1-T3 断线重连续传路径。
    const answer = mockAnswerFor(question);
    const assistantAt = appendHistory(sessionId, question, answer);
    const frames = [
      // thinking:content 只放占位文本(Agent 名),与契约安全红线一致
      sseFrame({
        event_type: "thinking",
        sequence: 1,
        session_id: sessionId,
        agent: "supervisor",
        content: "supervisor",
      }),
      // tool_call:摘要事件,不含工具参数
      sseFrame({
        event_type: "tool_call",
        sequence: 2,
        session_id: sessionId,
        agent: "supervisor",
        tool_name: "search_knowledge",
        success: null,
      }),
      // tool_result:摘要事件 + 耗时
      sseFrame({
        event_type: "tool_result",
        sequence: 3,
        session_id: sessionId,
        agent: "supervisor",
        tool_name: "search_knowledge",
        success: true,
        duration_ms: 12,
      }),
      // message_end:content 为最终消息全文,message 与 getSessionMessages
      // 返回的历史消息同构(chat-store 优先取 event.message)
      sseFrame({
        event_type: "message_end",
        sequence: 4,
        session_id: sessionId,
        agent: "supervisor",
        content: answer,
        message: {
          role: "assistant",
          content: answer,
          agent: "supervisor",
          created_at: assistantAt,
        },
        citations: null,
      }),
    ];
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: frames.join(""),
    });
  });

  // ── POST /chat(同步,降级路径与 API 直测使用) ──
  await page.route("**/chat", async (route) => {
    const body = JSON.parse(route.request().postData() ?? "{}") as {
      message?: string;
      session_id?: string;
    };
    const sessionId = body.session_id ?? "mock-session";
    const question = body.message ?? "";
    const answer = mockAnswerFor(question);
    const assistantAt = appendHistory(sessionId, question, answer);
    await route.fulfill({
      status: 200,
      headers: jsonHeaders(),
      body: JSON.stringify({
        session_id: sessionId,
        current_agent: "supervisor",
        events: [
          {
            event_type: "thinking",
            sequence: 1,
            session_id: sessionId,
            agent: "supervisor",
          },
        ],
        message: {
          role: "assistant",
          content: answer,
          agent: "supervisor",
          created_at: assistantAt,
        },
        // 用例 2:同步响应携带待审批 → store applyChatResponse 设置
        // pendingHandoff → 审批卡片出现
        pending_handoff: mockPendingHandoff,
        references: null,
        run_error: null,
        task_plan: null,
        task_results: null,
      }),
    });
  });

  // ── GET /sessions/{id}/messages → Message[](直接返回数组) ──
  await page.route("**/sessions/*/messages", async (route) => {
    const sessionId = sessionIdFromPath(route);
    await route.fulfill({
      status: 200,
      headers: jsonHeaders(),
      body: JSON.stringify(mockMessagesBySession.get(sessionId) ?? []),
    });
  });

  // ── /sessions/{id}/handoff(GET 查询 + POST 决策) ──
  await page.route("**/sessions/*/handoff", async (route) => {
    const sessionId = sessionIdFromPath(route);
    if (route.request().method() === "GET") {
      // PendingHandoffResponse(仅 decideHandoff 的 handoff_not_pending
      // 兜底路径会调用)
      await route.fulfill({
        status: 200,
        headers: jsonHeaders(),
        body: JSON.stringify({
          session_id: sessionId,
          pending_handoff: mockPendingHandoff,
        }),
      });
      return;
    }
    // POST 决策(confirm/reject/modify):一律清空待审批,返回无
    // handoff 的 ChatResponse → store 置空 pendingHandoff → 卡片卸载
    mockPendingHandoff = null;
    await route.fulfill({
      status: 200,
      headers: jsonHeaders(),
      body: JSON.stringify({
        session_id: sessionId,
        events: [],
        message: null,
        current_agent: null,
        pending_handoff: null,
        references: null,
        run_error: null,
        task_plan: null,
        task_results: null,
      }),
    });
  });

  // ── POST /sessions/{id}/archive → Session(注意:api-client 期望
  //    返回更新后的 Session,不是 204 空响应) ──
  await page.route("**/sessions/*/archive", async (route) => {
    const sessionId = sessionIdFromPath(route);
    const session = mockSessions.find((item) => item.session_id === sessionId);
    if (session) {
      session.archived = true;
    }
    await route.fulfill({
      status: 200,
      headers: jsonHeaders(),
      body: JSON.stringify(
        session ?? {
          archived: true,
          created_at: new Date().toISOString(),
          session_id: sessionId,
          user_id: "demo-user",
        },
      ),
    });
  });

  // ── /sessions(GET 列表 + POST 创建) ──
  await page.route("**/sessions", async (route) => {
    if (route.request().method() === "POST") {
      sessionCounter += 1;
      const session = mockSession(`mock-session-${sessionCounter}`);
      mockSessions = [session, ...mockSessions];
      await route.fulfill({
        status: 200,
        headers: jsonHeaders(),
        body: JSON.stringify(session),
      });
      return;
    }
    // GET:listSessions(includeArchived) —— 归档视图带 include_archived=true
    const includeArchived =
      new URL(route.request().url()).searchParams.get("include_archived") ===
      "true";
    const list = includeArchived
      ? mockSessions
      : mockSessions.filter((item) => !item.archived);
    await route.fulfill({
      status: 200,
      headers: jsonHeaders(),
      body: JSON.stringify(list),
    });
  });
}
