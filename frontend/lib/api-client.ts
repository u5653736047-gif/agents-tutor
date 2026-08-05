import type { components, paths } from "@/contracts/api.generated";
import {
  streamChat as streamChatImpl,
  streamChatWithRetry as streamChatWithRetryImpl,
  type StreamChatOptions,
  type StreamRetryOptions,
} from "./stream-client";

export type ApiErrorCode = components["schemas"]["ApiErrorCode"];
export type ChatRequest = components["schemas"]["ChatRequest"];
export type ChatResponse = paths["/chat"]["post"]["responses"][200]["content"]["application/json"];
export type CreateSessionRequest = components["schemas"]["CreateSessionRequest"];
// D2-T4:审批决策放开为全字段契约(action/interrupt_id + modify 的
// target_agent/task_content),由 store 按 action 构造合法组合。
export type HandoffDecision = components["schemas"]["HandoffDecisionRequest"];
export type Message = components["schemas"]["Message"];
export type PendingHandoffResponse = components["schemas"]["PendingHandoffResponse"];
export type Session = components["schemas"]["Session"];
// D6-T2:反馈评分方向与受理响应,直接取生成契约(单一数据源)
export type FeedbackRating = components["schemas"]["FeedbackRating"];
export type FeedbackResponse = components["schemas"]["FeedbackResponse"];
// D6-T2:反馈提交入参(camelCase 调用侧语义;发送前由 submitFeedback
// 转成契约 snake_case:session_id/message_id/comment/error_code)
export type FeedbackInput = {
  sessionId: string;
  messageId?: string;
  rating: FeedbackRating;
  comment?: string;
  errorCode?: string;
};
export type FeedbackResult = { received: boolean };
// D6-T4:知识库检索结果——直接取契约 KnowledgeSearchResponse(单一数据源):
// hits 元素为 SearchHitDto { summary, score, citation },citation 为
// Citation { document_id, source, page, chunk_id }。响应字段是契约
// snake_case,request() 不做转换(Session/Message 等先例一致),页面按
// snake_case 字段直接读取。
export type KnowledgeSearchResult = components["schemas"]["KnowledgeSearchResponse"];

const defaultApiBaseUrl = "http://127.0.0.1:8000";

export const apiBaseUrl = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? defaultApiBaseUrl
).replace(/\/+$/, "");
export const DEMO_USER_ID = "demo-user";
export const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;

type ApiClientErrorOptions = {
  code: ApiErrorCode | null;
  status: number | null;
};

export class ApiClientError extends Error {
  readonly code: ApiErrorCode | null;
  readonly status: number | null;

  constructor(message: string, { code, status }: ApiClientErrorOptions) {
    super(message);
    this.name = "ApiClientError";
    this.code = code;
    this.status = status;
  }
}

export type ApiClient = {
  archiveSession(sessionId: string): Promise<Session>;
  createSession(payload?: CreateSessionRequest): Promise<Session>;
  decideHandoff(sessionId: string, decision: HandoffDecision): Promise<ChatResponse>;
  getPendingHandoff(sessionId: string): Promise<PendingHandoffResponse>;
  getSessionMessages(sessionId: string): Promise<Message[]>;
  listSessions(includeArchived?: boolean): Promise<Session[]>;
  sendChat(payload: ChatRequest): Promise<ChatResponse>;
  // D6-T2:提交用户反馈(点赞/点踩+纠错)。与主对话接口解耦:错误与
  // 其它接口一样归一为 ApiClientError,由调用方(FeedbackButtons)
  // 决定呈现方式,不进入主流程错误状态。
  submitFeedback(input: FeedbackInput): Promise<FeedbackResult>;
  // D6-T4:知识库检索测试(教师端)——独立接口,不进入主会话 store。
  // 入参 camelCase(query/topK),发送前转契约 snake_case top_k;
  // 响应 { hits } 直接透传,错误归一为 ApiClientError。
  searchKnowledge(input: { query: string; topK?: number }): Promise<KnowledgeSearchResult>;
  streamChat(
    options: Omit<StreamChatOptions, "baseUrl" | "fetchImpl" | "userId">,
  ): Promise<void>;
  // D1-T3:断线重连与消息补发——指数退避重试 + fromSequence 续传。
  // 重试参数(maxRetries 等)由调用方传入;baseUrl/userId/fetchImpl/
  // timeoutMs 等基础设施配置与 streamChat 一样由注入配置填充。
  streamChatWithRetry(
    options: Omit<
      StreamChatOptions & StreamRetryOptions,
      "baseUrl" | "fetchImpl" | "userId"
    >,
  ): Promise<void>;
};

export type ApiClientOptions = {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  userId?: string;
};

type RequestConfig = Required<Pick<ApiClientOptions, "fetchImpl" | "timeoutMs" | "userId">> & {
  baseUrl: string;
};

function errorDetail(payload: unknown): ApiClientErrorOptions | null {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) {
    return null;
  }

  const detail = payload.detail;
  if (!detail || typeof detail !== "object") {
    return null;
  }

  const { error_code: code, message } = detail as {
    error_code?: unknown;
    message?: unknown;
  };
  if (typeof code !== "string" || typeof message !== "string") {
    return null;
  }

  return { code: code as ApiErrorCode, status: null };
}

async function readJson(response: Response): Promise<unknown | undefined> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

async function request<T>(
  config: RequestConfig,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), config.timeoutMs);
  const headers = new Headers({
    Accept: "application/json",
    "X-User-Id": config.userId,
  });
  if (init.body) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await config.fetchImpl(`${config.baseUrl}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
  } catch {
    throw new ApiClientError(
      controller.signal.aborted ? "请求超时，请稍后重试。" : "网络请求失败，请检查服务是否可用。",
      { code: null, status: null },
    );
  } finally {
    clearTimeout(timeout);
  }

  const payload = await readJson(response);
  if (!response.ok) {
    const detail = errorDetail(payload);
    throw new ApiClientError(
      detail ? (payload as { detail: { message: string } }).detail.message : "请求失败，请稍后重试。",
      { code: detail?.code ?? null, status: response.status },
    );
  }
  if (payload === undefined) {
    throw new ApiClientError("服务响应格式无效。", { code: null, status: response.status });
  }

  return payload as T;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const config: RequestConfig = {
    baseUrl: (options.baseUrl ?? apiBaseUrl).replace(/\/+$/, ""),
    fetchImpl: options.fetchImpl ?? fetch,
    timeoutMs: options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS,
    userId: options.userId ?? DEMO_USER_ID,
  };

  return {
    archiveSession: (sessionId) =>
      request<Session>(config, `/sessions/${encodeURIComponent(sessionId)}/archive`, {
        method: "POST",
      }),
    createSession: (payload: CreateSessionRequest = {}) =>
      request<Session>(config, "/sessions", {
        body: JSON.stringify(payload),
        method: "POST",
      }),
    decideHandoff: (sessionId, decision) =>
      request<ChatResponse>(config, `/sessions/${encodeURIComponent(sessionId)}/handoff`, {
        body: JSON.stringify(decision),
        method: "POST",
      }),
    getPendingHandoff: (sessionId) =>
      request<PendingHandoffResponse>(
        config,
        `/sessions/${encodeURIComponent(sessionId)}/handoff`,
      ),
    getSessionMessages: (sessionId) =>
      request<Message[]>(config, `/sessions/${encodeURIComponent(sessionId)}/messages`),
    listSessions: (includeArchived = false) =>
      request<Session[]>(
        config,
        includeArchived ? "/sessions?include_archived=true" : "/sessions",
      ),
    sendChat: (payload) =>
      request<ChatResponse>(config, "/chat", {
        body: JSON.stringify(payload),
        method: "POST",
      }),
    // D6-T2:反馈接口——只发契约 FeedbackRequest 的脱敏字段(不含消息
    // 全文);可选字段未传不落键(契约可空)。错误归一由 request() 完成
    // (非 200 抛 ApiClientError,含 errorDetail 解析)。
    submitFeedback: (input) =>
      request<FeedbackResponse>(config, "/feedback", {
        body: JSON.stringify({
          session_id: input.sessionId,
          rating: input.rating,
          ...(input.messageId !== undefined ? { message_id: input.messageId } : {}),
          ...(input.comment !== undefined ? { comment: input.comment } : {}),
          ...(input.errorCode !== undefined ? { error_code: input.errorCode } : {}),
        }),
        method: "POST",
      }).then((response) => ({ received: response.received ?? false })),
    // D6-T4:知识库检索——body 按契约 snake_case 发送(topK 未传默认
    // 5);响应 { hits } 直接透传(不做字段转换);非 200 抛
    // ApiClientError(含 errorDetail 解析),与其它接口一致。
    searchKnowledge: (input) =>
      request<KnowledgeSearchResult>(config, "/knowledge/search", {
        body: JSON.stringify({
          query: input.query,
          top_k: input.topK ?? 5,
        }),
        method: "POST",
      }),
    // 流式对话:baseUrl/userId/fetchImpl/timeoutMs 由注入配置填充,
    // 调用方只需传 sessionId/message/onEvent(及可选的 signal)。
    streamChat: (options) =>
      streamChatImpl({
        baseUrl: config.baseUrl,
        fetchImpl: config.fetchImpl,
        timeoutMs: config.timeoutMs,
        userId: config.userId,
        ...options,
      }),
    // D1-T3 重试版流式对话:同样由注入配置填充基础设施参数,额外支持
    // 断线重连(指数退避 + fromSequence 续传,实现见 stream-client)。
    streamChatWithRetry: (options) =>
      streamChatWithRetryImpl({
        baseUrl: config.baseUrl,
        fetchImpl: config.fetchImpl,
        timeoutMs: config.timeoutMs,
        userId: config.userId,
        ...options,
      }),
  };
}

export const apiClient = createApiClient();
