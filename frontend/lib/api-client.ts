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
export type HandoffDecision = Pick<
  components["schemas"]["HandoffDecisionRequest"],
  "action" | "interrupt_id"
>;
export type Message = components["schemas"]["Message"];
export type PendingHandoffResponse = components["schemas"]["PendingHandoffResponse"];
export type Session = components["schemas"]["Session"];

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
