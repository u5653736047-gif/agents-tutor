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
export type SessionProcess = components["schemas"]["SessionProcess"];
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
// D6-T6:知识库文档管理——上传回执与列表条目直接取契约(单一数据源):
// KnowledgeDocumentUploadResponse 与 KnowledgeDocumentListEntry 同构
// (document_id/source/page_count?/chunk_count?),page_count/chunk_count
// 可空(txt 无页概念、core 未接清单能力时留空),页面用 "—" 兜底。
export type KnowledgeDocumentUploadResponse =
  components["schemas"]["KnowledgeDocumentUploadResponse"];
export type KnowledgeDocumentListResponse =
  components["schemas"]["KnowledgeDocumentListResponse"];
export type KnowledgeDocumentEntry = components["schemas"]["KnowledgeDocumentListEntry"];
// D7-T2:聊天附件——上传回执与附件引用直接取契约(单一数据源):
// FileUploadResponse(file_id/name/content_type/size/url)与 Attachment
// (file_id/name/content_type/size)同源;字段均为契约单字段名(无多词
// 复合),调用侧直接透传,不做 camelCase 转换。
export type FileUploadReceipt = components["schemas"]["FileUploadResponse"];
export type AttachmentInput = components["schemas"]["Attachment"];
// D6-T7:学习进度基础统计——直接取契约 StatsOverview(单一数据源):
// session_count/message_count/agent_answer_counts(角色字符串→计数)/
// last_activity_at(ISO 时间戳或 null,无活动为 null)。响应字段是
// 契约 snake_case,request() 不做转换,页面按 snake_case 直接读取。
export type StatsOverview = components["schemas"]["StatsOverview"];

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
  getSessionProcess(sessionId: string): Promise<SessionProcess>;
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
  // D6-T6:知识库文档管理(上传/列表/删除)——教师端管理页使用。
  // uploadDocument 走 multipart/form-data(field 名 "file",见 request()
  // 对 FormData 的处理注释);onProgress 仅回调 0/1 里程碑(fetch 无原生
  // 上传进度事件,真实百分比需 XMLHttpRequest,本期不做)。
  deleteDocument(documentId: string): Promise<void>;
  listDocuments(): Promise<KnowledgeDocumentListResponse>;
  uploadDocument(
    file: File,
    onProgress?: (fraction: number) => void,
  ): Promise<KnowledgeDocumentUploadResponse>;
  // D6-T7:学习进度基础统计(只读聚合)——独立接口,不进入主会话 store
  // (与 searchKnowledge 同一隔离哲学)。GET /stats/overview,响应
  // 直接透传契约 StatsOverview;错误归一为 ApiClientError。
  getStatsOverview(): Promise<StatsOverview>;
  // D7-T2:聊天附件——uploadFile 走 multipart/form-data(field 名
  // "file",与 uploadDocument 同一 FormData 通道,错误归一为
  // ApiClientError);getFileUrl 纯字符串拼接(不 fetch),供
  // <img>/<a> 直接使用(file_id 为服务端生成的 uuid 安全段,
  // encodeURIComponent 兜底防路径注入)。
  getFileUrl(fileId: string): string;
  uploadFile(file: File): Promise<FileUploadReceipt>;
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
  // D6-T6:FormData(文档上传)不设 Content-Type——浏览器会自动带
  // multipart/form-data; boundary,强设 application/json 会破坏分界
  // 导致 422。其余 JSON body 保持原行为。
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  // 2026-08-07 prod 验收事故根因修复:必须解构后再调用 fetchImpl。
  // 浏览器原生 fetch 是 WebIDL 方法,要求 this 为 window/undefined;
  // 直接 config.fetchImpl(...) 成员调用会把 this 绑定为 config 对象,
  // 原生 fetch 同步抛 "Failed to execute 'fetch' on 'Window': Illegal
  // invocation" → request() catch → 所有 API 请求静默失败且零网络活动
  // (带扩展的浏览器因扩展 hook 了 window.fetch 为普通函数反而正常,
  // 掩盖了此 bug;Playwright/无痕等干净浏览器 100% 复现)。
  const fetchImpl = config.fetchImpl;
  let response: Response;
  try {
    response = await fetchImpl(`${config.baseUrl}${path}`, {
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
    // D6-T6:204 No Content(如 DELETE 文档)允许空响应体,不算格式错误。
    // 注意:该放行是 request() 的全局语义——契约上仅 DELETE 文档返回
    // 204;若后端对其它接口错误返回 204,原「服务响应格式无效」会被
    // 静默吞掉(调用方会拿到 undefined),各接口契约约束在服务端。
    if (response.status === 204) {
      return undefined as T;
    }
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
    getSessionProcess: (sessionId) =>
      request<SessionProcess>(config, `/sessions/${encodeURIComponent(sessionId)}/process`),
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
    // D6-T6:文档上传——body 为 FormData(field 名 "file"),request() 对
    // FormData 不设 Content-Type(见 request 注释),由浏览器自动带
    // multipart boundary。onProgress 仅 0/1 里程碑(成功 1、失败回 0),
    // 页面以「上传中…」禁用态表达进度,不依赖进度回调。
    uploadDocument: async (file, onProgress) => {
      onProgress?.(0);
      try {
        const body = new FormData();
        body.append("file", file);
        const response = await request<KnowledgeDocumentUploadResponse>(
          config,
          "/knowledge/documents",
          { body, method: "POST" },
        );
        onProgress?.(1);
        return response;
      } catch (error) {
        onProgress?.(0);
        throw error;
      }
    },
    // D6-T6:文档清单——GET 直接透传契约响应(documents 数组)。
    listDocuments: () =>
      request<KnowledgeDocumentListResponse>(config, "/knowledge/documents"),
    // D6-T6:删除文档——204 空响应体由 request() 放行(见 request 注释)。
    deleteDocument: (documentId) =>
      request<void>(
        config,
        `/knowledge/documents/${encodeURIComponent(documentId)}`,
        { method: "DELETE" },
      ),
    // D6-T7:学习进度——GET /stats/overview,响应直接透传契约字段
    // (snake_case 原样,与 listDocuments 等先例一致)。
    getStatsOverview: () => request<StatsOverview>(config, "/stats/overview"),
    // D7-T2:附件上传——与 uploadDocument 同一 FormData 模式(field 名
    // "file",request() 对 FormData 不设 Content-Type,浏览器自动带
    // multipart boundary)。无进度回调:上传中态由组件 pendingFiles
    // 状态表达,失败抛 ApiClientError(含 errorDetail 解析)。
    uploadFile: (file) => {
      const body = new FormData();
      body.append("file", file);
      return request<FileUploadReceipt>(config, "/files", { body, method: "POST" });
    },
    // D7-T2:受控下载 URL——仅拼接不 fetch:<img>/<a> 直接使用。
    // 注:GET /files/{file_id} 按 X-User-Id 用户隔离校验(匿名访问
    // 他人文件 404),浏览器直链无法带自定义头,该缺口由 D7-T3 的
    // 展示层决定如何补(本期只提供 URL 拼接)。
    getFileUrl: (fileId) => `${config.baseUrl}/files/${encodeURIComponent(fileId)}`,
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

// D7-T2:模块级便捷入口——组件(chat-input)直接 import,代理默认
// apiClient(同一注入配置);需要注入自定义 fetch/baseUrl 的场景走
// createApiClient 的接口方法。
export function uploadFile(file: File): Promise<FileUploadReceipt> {
  return apiClient.uploadFile(file);
}

export function getFileUrl(fileId: string): string {
  return apiClient.getFileUrl(fileId);
}
