"use client";

import { create, type StoreApi } from "zustand";

import {
  ApiClientError,
  apiClient,
  type ApiClient,
  type AttachmentInput,
  type ChatResponse,
  type FeedbackInput,
  type HandoffDecision,
  type Message,
  type PendingHandoffResponse,
  type PendingToolApprovalResponse,
  type Session,
  type ToolApprovalDecision,
} from "../lib/api-client";
import type { components } from "../contracts/api.generated";

type ChatStoreClient = Pick<
  ApiClient,
  "archiveSession" | "createSession" | "getSessionMessages" | "listSessions" | "sendChat"
> & {
  // 正式客户端会从 checkpoint 恢复过程；可选以兼容只关注消息的测试替身。
  getSessionProcess?: ApiClient["getSessionProcess"];
  // 正式客户端可为当前会话追加只读授权目录；可选以兼容旧测试替身。
  addWorkspaceRoot?: ApiClient["addWorkspaceRoot"];
  // D2-T3:审批接口——可选:既有测试注入的 stub 未实现,正式 apiClient
  // 一定实现;decideHandoff action 在未注入时直接跳过(与 streamChat 同模式)
  decideHandoff?: ApiClient["decideHandoff"];
  getPendingHandoff?: ApiClient["getPendingHandoff"];
  decideToolApproval?: ApiClient["decideToolApproval"];
  getPendingToolApproval?: ApiClient["getPendingToolApproval"];
  streamToolApproval?: ApiClient["streamToolApproval"];
  // 可选:既有测试注入的 stub 未实现流式通道,正式 apiClient 一定实现
  streamChat?: ApiClient["streamChat"];
  // D1-T3:断线重连通道(指数退避重试 + fromSequence 续传)。store 优先
  // 使用它;未注入(旧测试 stub)时退回底层 streamChat,失败不降级
  // (保持 D1-T1 行为)。
  streamChatWithRetry?: ApiClient["streamChatWithRetry"];
  // D6-T2:反馈提交——可选:既有测试注入的 stub 未实现,正式 apiClient
  // 一定实现;submitFeedback action 在未注入时直接跳过(与 decideHandoff
  // 同模式)。
  submitFeedback?: ApiClient["submitFeedback"];
};
// NonNullable:响应字段可选(含 undefined),store 语义统一为 null
// (所有写入点都用 ?? null 归一化)——否则组件 props 会收到 undefined。
type PendingHandoff = NonNullable<PendingHandoffResponse["pending_handoff"]>;
type PendingToolApproval = NonNullable<
  PendingToolApprovalResponse["pending_tool_approval"]
>;
type RunError = ChatResponse["run_error"];
type RunEvent = components["schemas"]["RunEvent"];
type StreamEvent = components["schemas"]["StreamEvent"];
type AgentRole = components["schemas"]["AgentRole"];
type WorkerAgentRole = components["schemas"]["WorkerAgentRole"];
// D2-T4:审批修改字段——组件层 camelCase 语义,发送前由 store 转契约 snake_case
export type HandoffModifications = {
  targetAgent?: WorkerAgentRole;
  taskContent?: string;
};
// D2-T2:任务计划与执行结果(ChatResponse.task_plan / task_results)
type TaskPlan = components["schemas"]["TaskPlan"];
type TaskResult = components["schemas"]["TaskResult"];
// D3-T4:回答引用(ChatResponse.references)——与既有字段同一取型方式,
// 直接取生成契约,保持单一数据源
type Citation = components["schemas"]["Citation"];
// P2-12:本轮批改结论(ChatResponse.grading / StreamEvent.grading)——
// 与 references 同一取型与归一化口径;历史轮次的批改经消息级
// Message.grading 元数据恢复(刷新/切会话不丢,pi 审查 🟡4)。
type GradingResult = NonNullable<components["schemas"]["GradingResultDto"]>;

// D1-T3:流式通道重试上限(重试次数,不含首次尝试),传给
// streamChatWithRetry 的 maxRetries;耗尽后向用户报告连接错误，绝不
// 自动改走同步通道重发同一条消息。
const _STREAM_RETRY_LIMIT = 3;

export type ChatStore = {
  addWorkspaceRoot(path: string): Promise<Session | null>;
  archiveSession(sessionId: string): Promise<void>;
  // D4-T3:停止生成——abort 当前流式请求(仅对流式通道生效,同步
  // 通道无取消能力);无活跃流时为 no-op。
  cancelStreaming(): void;
  clearConversationState(): void;
  clearRequestError(): void;
  createSession(workspaceRoot?: string): Promise<Session | null>;
  currentAgent: AgentRole | null;
  currentSessionId: string | null;
  // D2-T3:审批决策——决定(确认/拒绝/修改)与状态字段
  // D2-T4:modify 时携带 modifications(目标 Agent / 任务内容),store 组装请求体
  decideHandoff(
    action: "confirm" | "reject" | "modify",
    modifications?: HandoffModifications,
  ): Promise<void>;
  decideToolApproval(action: "confirm" | "reject"): Promise<void>;
  degradedNotice: string | null;
  events: (RunEvent | StreamEvent)[];
  // P2-12:本轮批改结论(null = 非批改轮,与后端「无批改不携带」契约
  // 一致;GradingCard 组件零渲染降级)
  grading: GradingResult | null;
  isDecidingHandoff: boolean;
  isDecidingToolApproval: boolean;
  isLoadingMessages: boolean;
  isLoadingSessions: boolean;
  isSending: boolean;
  isStreaming: boolean;
  lastSentMessage: string | null;
  loadCurrentSessionMessages(): Promise<void>;
  messages: Message[];
  pendingHandoff: PendingHandoff | null;
  pendingToolApproval: PendingToolApproval | null;
  // D3-T4:本轮回答的引用列表(null = 无引用,与后端「无引用不携带」
  // 契约一致;组件层零渲染降级)
  references: Citation[] | null;
  // UX-20260808#1:quiet=true 时后台静默换新(不切换 isLoadingSessions、
  // 不闪骨架)——用于消息完成后补拉标题等「列表已在展示」的场景;
  // 缺省 false 维持挂载/重试/归档切换时的骨架加载态。
  refreshSessions(options?: { quiet?: boolean }): Promise<void>;
  requestError: ApiClientError | null;
  retryLastMessage(): Promise<void>;
  runError: RunError | null;
  selectSession(sessionId: string | null): void;
  // D7-T2:附件引用列表(camelCase 语义即契约字段,file_id 等单字段名
  // 无转换)。可选参数向后兼容:既有调用不传,行为不变。
  sendMessage(message: string, attachments?: AttachmentInput[]): Promise<void>;
  sessions: Session[];
  // D4-T7:归档视图开关——true 时 refreshSessions 带 include_archived
  // 拉取归档会话;setShowArchived 切换后立即按新视图重新拉取。
  setShowArchived(show: boolean): void;
  showArchived: boolean;
  // D7-T2:附件引用列表(与 sendMessage 同一语义)。可选参数向后兼容。
  streamSendMessage(message: string, attachments?: AttachmentInput[]): Promise<void>;
  streamingAgent: AgentRole | null;
  streamingMessage: Message | null;
  // D6-T2:提交用户反馈(点赞/点踩+纠错)。与主对话流程解耦:不写
  // requestError,失败由调用方(FeedbackButtons)catch 后在组件内
  // 错误行呈现——反馈失败静默降级,不阻塞对话。
  submitFeedback(input: FeedbackInput): Promise<void>;
  taskPlan: TaskPlan | null;
  taskResults: TaskResult[] | null;
};

function emptyConversationState() {
  return {
    currentAgent: null,
    degradedNotice: null as string | null,
    events: [] as (RunEvent | StreamEvent)[],
    isDecidingHandoff: false,
    isDecidingToolApproval: false,
    isLoadingMessages: false,
    isSending: false,
    isStreaming: false,
    lastSentMessage: null,
    messages: [] as Message[],
    pendingHandoff: null,
    pendingToolApproval: null,
    // P2-12:批改结论与引用同一轮次语义(对应最后一轮回答,不残留)
    grading: null,
    // D3-T4:引用随轮次清空(引用对应最后一轮回答,切会话/新建会话不残留)
    references: null,
    runError: null,
    streamingAgent: null,
    streamingMessage: null,
    taskPlan: null,
    taskResults: null,
  };
}

function asApiClientError(error: unknown): ApiClientError {
  if (error instanceof ApiClientError) {
    return error;
  }

  return new ApiClientError("请求失败，请稍后重试。", { code: null, status: null });
}

// D2-T3:sendMessage 与 decideHandoff 共用的 ChatResponse 字段合并。
// events 重置为本轮增量(D2-T2 语义:ChatResponse.events 按 previous_sequence
// 过滤,累积会让时间线跨 run 交错);sendMessage 调用前已清空 events,
// 因此与原先的追加写法等价(行为不变)。
function applyChatResponse(
  state: ChatStore,
  response: ChatResponse,
): Partial<ChatStore> {
  return {
    currentAgent: response.current_agent ?? null,
    events: [...(response.events ?? [])],
    pendingHandoff: response.pending_handoff ?? null,
    pendingToolApproval: response.pending_tool_approval ?? null,
    // P2-12:批改结论同一归一化口径(缺失 → null,组件零渲染)
    grading: response.grading ?? null,
    // D3-T4:响应缺失(undefined/null)统一归一为 null,store 语义与
    // 其它可选字段一致,组件层收到 null 时零渲染
    references: response.references ?? null,
    runError: response.run_error ?? null,
    taskPlan: response.task_plan ?? null,
    taskResults: response.task_results ?? null,
  };
}

type ChatStoreSet = StoreApi<ChatStore>["setState"];
type ChatStoreGet = StoreApi<ChatStore>["getState"];
type StreamDispatchContext = { activeSupervisorMessageId: string | null };

function dispatchStreamEvent(
  set: ChatStoreSet,
  get: ChatStoreGet,
  sessionId: string,
  event: StreamEvent,
  context: StreamDispatchContext,
) {
  if (get().currentSessionId !== sessionId) {
    return;
  }
  switch (event.event_type) {
    case "thinking":
      set((state) => ({
        events: [...state.events, event],
        streamingAgent: event.agent ?? null,
      }));
      break;
    case "reasoning": {
      const messageId =
        event.message_id ?? `${event.agent ?? "agent"}-reasoning-${event.sequence}`;
      const delta = event.content ?? "";
      set((state) => {
        const existingIndex = state.events.findIndex(
          (item) =>
            item.event_type === "reasoning" &&
            "message_id" in item &&
            item.message_id === messageId,
        );
        if (existingIndex < 0) {
          return {
            events: [...state.events, { ...event, message_id: messageId }],
            streamingAgent: event.agent ?? null,
          };
        }
        const events = [...state.events];
        const existing = events[existingIndex];
        const previousContent =
          existing && "content" in existing ? (existing.content ?? "") : "";
        events[existingIndex] = {
          ...event,
          content: event.is_delta === false ? delta : `${previousContent}${delta}`,
          message_id: messageId,
          sequence: existing?.sequence ?? event.sequence,
        };
        return { events, streamingAgent: event.agent ?? null };
      });
      break;
    }
    case "tool_call":
    case "tool_result":
    case "tool_output":
      set((state) => ({ events: [...state.events, event] }));
      break;
    case "approval_required":
      set({
        pendingToolApproval: event.pending_tool_approval ?? null,
        streamingAgent: event.agent ?? null,
      });
      break;
    case "message_delta": {
      const delta = event.content ?? "";
      if (event.agent === "supervisor") {
        const messageId = event.message_id ?? "supervisor";
        const continuesCurrent = context.activeSupervisorMessageId === messageId;
        context.activeSupervisorMessageId = messageId;
        set((state) => ({
          streamingAgent: "supervisor",
          streamingMessage: {
            agent: "supervisor",
            content: `${continuesCurrent ? (state.streamingMessage?.content ?? "") : ""}${delta}`,
            created_at: undefined,
            role: "assistant",
          },
        }));
        break;
      }

      set((state) => {
        const messageId =
          event.message_id ?? `${event.agent ?? "agent"}-${event.sequence}`;
        const existingIndex = state.events.findIndex(
          (item) =>
            item.event_type === "message_delta" &&
            "message_id" in item &&
            item.message_id === messageId,
        );
        if (existingIndex < 0) {
          return {
            events: [...state.events, event],
            streamingAgent: event.agent ?? null,
          };
        }
        const events = [...state.events];
        const existing = events[existingIndex];
        const previousContent =
          existing && "content" in existing ? (existing.content ?? "") : "";
        events[existingIndex] = {
          ...event,
          content: `${previousContent}${delta}`,
          message_id: messageId,
        };
        return { events, streamingAgent: event.agent ?? null };
      });
      break;
    }
    case "agent_switch":
      set((state) => ({
        currentAgent: event.agent ?? null,
        events: [...state.events, event],
        streamingAgent: event.agent ?? null,
      }));
      break;
    case "message_end": {
      const streamed: Message = event.message ?? {
        agent: event.agent ?? undefined,
        content: event.content ?? "",
        created_at: undefined,
        role: "assistant",
      };
      set({
        streamingAgent: event.agent ?? null,
        streamingMessage: streamed,
        references: event.citations ?? null,
        // P2-12:流式 message_end 载荷携带本轮批改结论(与 citations 同位)
        grading: event.grading ?? null,
      });
      break;
    }
    case "error":
      set({
        runError: {
          agent: event.agent ?? undefined,
          error_code: event.error_code ?? "internal_error",
          message: "The request could not be completed.",
        },
      });
      break;
    case "done":
      break;
  }
}

export function createChatStore(client: ChatStoreClient = apiClient) {
  // D4-T3:当前流式请求的 AbortController,存于工厂闭包内(create
  // 调用外)——非响应式字段,不触发渲染;多个 store 实例各自持有
  // 自己的 controller,不会串(cancelStreaming 只 abort 本实例的流)。
  let activeStreamController: AbortController | null = null;
  // UX-20260807#2:refreshSessions 进行中去重改用非响应式闭包标志——
  // isLoadingSessions 初始值改为 true(修侧栏首帧闪空态)后不能再兼任
  // 「请求在飞」标记,否则首次挂载拉取会被守卫误拦。
  let sessionsRefreshInFlight = false;

  return create<ChatStore>()((set, get) => ({
    ...emptyConversationState(),
    currentSessionId: null,
    // UX-20260807#2:初始值改为 true——挂载 effect 才发起拉取,首帧
    // sessions 空且未加载会闪现「暂无会话」;初始 true 首帧即渲染侧栏
    // 骨架(与 knowledge 页 listLoading 初始 true 的先例一致)。
    isLoadingSessions: true,
    requestError: null,
    sessions: [],
    showArchived: false,
    addWorkspaceRoot: async (path) => {
      const sessionId = get().currentSessionId;
      if (!sessionId || !client.addWorkspaceRoot) {
        return null;
      }
      set({ requestError: null });
      try {
        const session = await client.addWorkspaceRoot(sessionId, path);
        set((state) => ({
          sessions: state.sessions.map((item) =>
            item.session_id === session.session_id ? session : item,
          ),
        }));
        return session;
      } catch (error) {
        set({ requestError: asApiClientError(error) });
        return null;
      }
    },
    archiveSession: async (sessionId) => {
      // D4-T3 review 修正:切会话时 abort 活跃流——旧流继续在后台
      // 跑完浪费算力,且「切走再切回」时旧流剩余事件会重新通过会话
      // 守卫写回污染新状态;abort 后 finally 的引用比对与会话守卫
      // 保证无副作用、不误清新流引用。
      activeStreamController?.abort();
      set({ requestError: null });
      try {
        await client.archiveSession(sessionId);
        set((state) => ({
          ...(state.currentSessionId === sessionId ? emptyConversationState() : {}),
          currentSessionId:
            state.currentSessionId === sessionId ? null : state.currentSessionId,
          sessions: state.sessions.filter((session) => session.session_id !== sessionId),
        }));
        // D4-T7:归档成功后以服务端为准刷新列表(本地乐观移除之外再
        // 拉一次)。未归档视图:该会话不再出现,与后端一致;归档视图:
        // include_archived=true 会把该会话重新带回来,保持「归档列表
        // = 服务端归档列表」语义。
        void get().refreshSessions();
      } catch (error) {
        set({ requestError: asApiClientError(error) });
      }
    },
    clearConversationState: () => set(emptyConversationState()),
    clearRequestError: () => set({ requestError: null }),
    // D4-T3:停止生成——abort 当前流(streamChatWithRetry 对调用方
    // abort 静默返回,streamSendMessage 的 catch 因 signal.aborted 走
    // 正常收尾路径:已收到的流式内容保留)。同步通道无取消能力,
    // 按钮仅对流式通道生效(D4-T3 定义)。
    cancelStreaming: () => {
      activeStreamController?.abort();
    },
    createSession: async (workspaceRoot) => {
      // D4-T3 review 修正:新建会话同样中止旧流(见 archiveSession 注释)。
      activeStreamController?.abort();
      set({ requestError: null });
      try {
        const session = await client.createSession(
          workspaceRoot === undefined ? {} : { workspace_root: workspaceRoot },
        );
        set((state) => ({
          ...emptyConversationState(),
          currentSessionId: session.session_id,
          sessions: [
            session,
            ...state.sessions.filter((item) => item.session_id !== session.session_id),
          ],
        }));
        return session;
      } catch (error) {
        set({ requestError: asApiClientError(error) });
        return null;
      }
    },
    loadCurrentSessionMessages: async () => {
      const sessionId = get().currentSessionId;
      if (!sessionId) {
        set({ ...emptyConversationState(), requestError: null });
        return;
      }

      set({ isLoadingMessages: true, requestError: null });
      try {
        const processSnapshot = client.getSessionProcess
          ? client.getSessionProcess(sessionId).catch(() => null)
          : Promise.resolve(null);
        const [messages, process] = await Promise.all([
          client.getSessionMessages(sessionId),
          processSnapshot,
        ]);
        if (get().currentSessionId === sessionId) {
          set({
            isLoadingMessages: false,
            messages,
            ...(process
              ? {
                  currentAgent: process.current_agent ?? null,
                  events: [...(process.events ?? [])],
                  pendingToolApproval: process.pending_tool_approval ?? null,
                  taskPlan: process.task_plan ?? null,
                  taskResults: process.task_results ?? null,
                }
              : {}),
          });
        }
      } catch (error) {
        if (get().currentSessionId === sessionId) {
          set({ isLoadingMessages: false, requestError: asApiClientError(error) });
        }
      }
    },
    refreshSessions: async (options) => {
      // D4-T5 review 修正:桌面静态侧栏与移动抽屉是两个组件实例,
      // 打开抽屉会再次触发挂载拉取——进行中去重,避免重复请求
      // (数据幂等,仅去网络开销;并发失败时下一次调用仍可重试,
      // 因为失败路径会复位 isLoadingSessions)。
      if (sessionsRefreshInFlight) {
        return;
      }
      sessionsRefreshInFlight = true;
      // UX-20260808#1:quiet 模式不动 isLoadingSessions 与 requestError
      // ——列表原地换新不闪骨架;后台补拉失败静默(标题下次刷新再补),
      // 绝不覆盖历史拉取等主流程刚写入的错误。
      const quiet = options?.quiet ?? false;
      set(quiet ? {} : { isLoadingSessions: true, requestError: null });
      try {
        // D4-T7:按当前归档视图拉取——showArchived=true 时带上
        // include_archived=true,归档会话可见(api-client 已支持该参数)。
        const sessions = await client.listSessions(get().showArchived);
        set(quiet ? { sessions } : { isLoadingSessions: false, sessions });
      } catch (error) {
        if (!quiet) {
          set({ isLoadingSessions: false, requestError: asApiClientError(error) });
        }
      } finally {
        sessionsRefreshInFlight = false;
      }
    },
    selectSession: (sessionId) => {
      // D4-T3 review 修正:切换会话中止旧流(见 archiveSession 注释)。
      activeStreamController?.abort();
      set({
        ...emptyConversationState(),
        currentSessionId: sessionId,
        requestError: null,
      });
    },
    // D2-T3:确认/拒绝待审批手递交接。
    // 成功时与 sendMessage 一样「POST + 拉全量」两段式,response 字段走
    // 公共 applyChatResponse;后端把 session_busy 放进成功响应的 run_error
    // (200,非 HTTP 错误),这里转成审批卡片的友好文案;409
    // handoff_not_pending 表示已被他人处理,清本地 pending 后 GET 兜底刷新。
    // D2-T4:支持修改决策。modify 的修改字段由组件以 camelCase 传入,
    // 这里转成契约 snake_case 组装进请求体;非 modify 一律不带修改字段
    // (与后端双分支校验对齐),守卫逻辑与 D2-T3 一致。
    decideHandoff: async (
      action: "confirm" | "reject" | "modify",
      modifications?: HandoffModifications,
    ) => {
      const sessionId = get().currentSessionId;
      const pending = get().pendingHandoff;
      const decide = client.decideHandoff;
      // 无会话 / 无待审批 / stub 未注入审批接口时直接跳过(不降级)
      if (!sessionId || !pending || !decide) {
        return;
      }

      const decision: HandoffDecision = {
        action,
        interrupt_id: pending.interrupt_id,
        ...(action === "modify" && modifications?.targetAgent
          ? { target_agent: modifications.targetAgent }
          : {}),
        ...(action === "modify" && modifications?.taskContent
          ? { task_content: modifications.taskContent }
          : {}),
      };
      // 本地校验:modify 至少携带一项修改(后端 422 语义前置;组件层也有
      // 校验,这里是防御性兜底,避免空白请求打到后端)
      if (
        action === "modify" &&
        decision.target_agent == null &&
        decision.task_content == null
      ) {
        set({
          isDecidingHandoff: false,
          requestError: new ApiClientError("请至少修改目标 Agent 或任务内容。", {
            code: "invalid_request",
            status: 422,
          }),
        });
        return;
      }

      set({ isDecidingHandoff: true, requestError: null });
      try {
        const response = await decide(sessionId, decision);
        if (get().currentSessionId === sessionId) {
          const messages = await client.getSessionMessages(sessionId);
          if (get().currentSessionId === sessionId) {
            const busyMessage =
              response.run_error?.error_code === "session_busy"
                ? new ApiClientError("会话正忙,请稍后重试。", {
                    code: "session_busy",
                    status: null,
                  })
                : null;
            set({
              ...applyChatResponse(get(), response),
              messages,
              isDecidingHandoff: false,
              ...(busyMessage ? { requestError: busyMessage } : {}),
            });
          }
        }
      } catch (error) {
        if (get().currentSessionId === sessionId) {
          const apiError = asApiClientError(error);
          if (apiError.code === "handoff_not_pending") {
            // 已被他人处理:清除本地 pending 并刷新(getPendingHandoff 兜底)
            set({ isDecidingHandoff: false, pendingHandoff: null });
            const refreshPending = client.getPendingHandoff;
            if (refreshPending) {
              try {
                const fresh = await refreshPending(sessionId);
                if (get().currentSessionId === sessionId) {
                  set({ pendingHandoff: fresh.pending_handoff ?? null });
                }
              } catch {
                // 刷新失败保持已清除状态
              }
            }
          } else if (apiError.code === "session_busy") {
            set({
              isDecidingHandoff: false,
              requestError: new ApiClientError("会话正忙,请稍后重试。", {
                code: apiError.code,
                status: apiError.status,
              }),
            });
          } else {
            set({ isDecidingHandoff: false, requestError: apiError });
          }
        }
      }
    },
    decideToolApproval: async (action: "confirm" | "reject") => {
      const sessionId = get().currentSessionId;
      const pending = get().pendingToolApproval;
      const streamDecision = client.streamToolApproval;
      const syncDecision = client.decideToolApproval;
      if (
        !sessionId ||
        !pending ||
        (!streamDecision && !syncDecision) ||
        get().isDecidingToolApproval
      ) {
        return;
      }

      const decision: ToolApprovalDecision = {
        action,
        interrupt_id: pending.interrupt_id,
      };
      const dispatchContext: StreamDispatchContext = {
        activeSupervisorMessageId: null,
      };
      set({
        isDecidingToolApproval: true,
        isStreaming: true,
        requestError: null,
        runError: null,
        streamingAgent: pending.request.agent_role,
        streamingMessage: null,
      });

      try {
        if (streamDecision) {
          await streamDecision({
            decision,
            onEvent: (event) =>
              dispatchStreamEvent(
                set,
                get,
                sessionId,
                event,
                dispatchContext,
              ),
            sessionId,
          });
        } else if (syncDecision) {
          const response = await syncDecision(sessionId, decision);
          if (get().currentSessionId === sessionId) {
            set((state) => ({
              ...applyChatResponse(state, response),
              events: [...state.events, ...(response.events ?? [])],
            }));
          }
        }

        if (get().currentSessionId === sessionId) {
          set((state) => ({
            isDecidingToolApproval: false,
            isStreaming: false,
            messages: state.streamingMessage
              ? [...state.messages, state.streamingMessage]
              : state.messages,
            pendingToolApproval:
              state.pendingToolApproval?.interrupt_id === pending.interrupt_id
                ? null
                : state.pendingToolApproval,
            streamingAgent: null,
            streamingMessage: null,
          }));
          await get().loadCurrentSessionMessages();
        }
      } catch (error) {
        if (get().currentSessionId === sessionId) {
          const apiError = asApiClientError(error);
          if (apiError.code === "tool_approval_not_pending") {
            set({
              isDecidingToolApproval: false,
              isStreaming: false,
              pendingToolApproval: null,
            });
            const refreshPending = client.getPendingToolApproval;
            if (refreshPending) {
              try {
                const fresh = await refreshPending(sessionId);
                if (get().currentSessionId === sessionId) {
                  set({
                    pendingToolApproval: fresh.pending_tool_approval ?? null,
                  });
                }
              } catch {
                // A stale approval stays cleared when the refresh also fails.
              }
            }
          } else {
            set({
              isDecidingToolApproval: false,
              isStreaming: false,
              requestError: apiError,
            });
          }
        }
      } finally {
        if (get().currentSessionId === sessionId) {
          set({ isDecidingToolApproval: false, isStreaming: false });
        }
      }
    },
    sendMessage: async (message, attachments) => {
      const sessionId = get().currentSessionId;
      if (!sessionId) {
        set({
          requestError: new ApiClientError("请先选择会话。", { code: null, status: null }),
        });
        return;
      }
      if (get().pendingToolApproval) {
        return;
      }

      // D4-T2:乐观更新——守卫通过后、调 sendChat 前,先把用户消息
      // 追加进 messages(UI 即时回显,不等网络往返)。created_at 用
      // undefined 占位,权威历史会整体替换;构造风格与流式
      // message_end 的兜底 Message 一致。
      const optimistic: Message = {
        agent: null,
        content: message,
        created_at: undefined,
        role: "user",
      };
      // D2-T5:发起前记录上一条消息,供失败后的「重新发送」入口使用
      // (守卫通过才记录;无会话时不覆盖旧值,与既有行为一致)。
      // 函数式 set 与乐观追加合并为一次更新:并发连续发送时各自基于
      // 最新 state 追加,乐观消息按调用顺序排列。
      set((state) => ({
        isSending: true,
        requestError: null,
        events: [],
        lastSentMessage: message,
        messages: [...state.messages, optimistic],
      }));
      try {
        const response = await client.sendChat({
          message,
          session_id: sessionId,
          // D7-T2:附件随消息提交(契约 ChatRequest.attachments,可空)。
          // 未传不落键——既有调用与后端骨架行为完全不变。
          ...(attachments && attachments.length > 0 ? { attachments } : {}),
        });
        const messages = await client.getSessionMessages(sessionId);
        if (get().currentSessionId === sessionId) {
          // D2-T3:response 字段合并抽到公共 applyChatResponse(与
          // decideHandoff 复用);isSending/messages 保持本处原有结构。
          // 乐观消息被权威历史整体替换(用户消息在后端历史中天然
          // 存在,一致即可,无需去重)。
          set((state) => ({
            ...applyChatResponse(state, response),
            isSending: false,
            messages,
          }));
        }
        // UX-20260808#1:消息落库后后端已按首条消息补标题——静默刷新
        // 会话列表(quiet 不闪骨架),侧栏尽快以标题替换 session_id。
        void get().refreshSessions({ quiet: true });
      } catch (error) {
        if (get().currentSessionId === sessionId) {
          // D4-T2:失败回滚——只移除本次追加的乐观消息。按对象引用
          // 从末尾向前找(只移除一条),避免误删历史中同内容同 role
          // 的用户消息;tsconfig lib 为 es2022,无 Array.findLastIndex
          // (ES2023),用循环等价实现。
          set((state) => {
            const messages = [...state.messages];
            let index = -1;
            for (let i = messages.length - 1; i >= 0; i -= 1) {
              if (messages[i] === optimistic) {
                index = i;
                break;
              }
            }
            if (index !== -1) {
              messages.splice(index, 1);
            }
            return { isSending: false, requestError: asApiClientError(error), messages };
          });
        }
      }
    },
    // D4-T7:切换归档视图。先更新状态、再按新状态重新拉取
    // (refreshSessions 内部从 get() 读 showArchived,set 之后立即调用
    // 必然拿到新值)。与 D4-T5 的进行中去重不冲突:去重守卫只挡
    // 「同一时刻并发」的重复请求,切换场景上一次拉取已完成;若恰逢
    // 拉取进行中,本次切换被跳过(列表保持旧视图),下一次切换或
    // 挂载拉取会纠正——可接受的轻微竞态,不做额外同步。
    setShowArchived: (show) => {
      set({ showArchived: show });
      void get().refreshSessions();
    },
    // D6-T2:提交反馈。action 只做转发:成功 resolve,失败原样抛给调用方
    // (FeedbackButtons 内 catch 后显示组件内错误行)——刻意不写
    // requestError,反馈独立于主对话流程,失败不阻塞对话、不污染
    // 主流程错误状态。stub 未注入 submitFeedback 时静默跳过
    // (与 decideHandoff 同模式)。
    submitFeedback: async (input) => {
      const submit = client.submitFeedback;
      if (!submit) {
        return;
      }
      await submit(input);
    },
    // D2-T5:重新发送上一条消息。通道选择与首次发送一致:client 有
    // 流式能力(streamChatWithRetry / streamChat)时走 streamSendMessage
    // (当前 UI 主通道),否则走 sendMessage(同步通道,既有降级语义)。
    // D7-T2:重发仅携带文本——附件引用是一次性提交的本地回执,失败
    // 重发场景不自动补带(附件文件已在服务端,孤儿回收是后端职责)。
    retryLastMessage: async () => {
      const message = get().lastSentMessage;
      if (!message) {
        return;
      }
      if (client.streamChatWithRetry || client.streamChat) {
        await get().streamSendMessage(message);
      } else {
        await get().sendMessage(message);
      }
    },
    streamSendMessage: async (message, attachments) => {
      const sessionId = get().currentSessionId;
      if (!sessionId) {
        set({
          requestError: new ApiClientError("请先选择会话。", { code: null, status: null }),
        });
        return;
      }
      if (get().pendingToolApproval) {
        return;
      }

      // 同一 store 同时只允许一个发送动作。UI 禁用需要一次 React
      // 重渲染才生效，双击/连续 Enter 仍可能在同一事件循环内进入两次；
      // 这里用同步状态做最终闸门，避免启动两个后端 run。
      if (get().isStreaming || get().isSending) {
        return;
      }

      // D7-T2:附件消息——stream-client 已扩展 attachments 透传(与同步
      // 通道同契约),正常走流式主通道获得流式体验。网络状态不确定时
      // 也不自动重发，避免同一条附件消息触发两个后端 run。

      // D1-T3:优先走带断线重连的通道(指数退避 + fromSequence 续传);
      // 未注入重试通道的旧 stub 退回底层 streamChat(D1-T1 行为不变)。
      const retryStream = client.streamChatWithRetry;
      const plainStream = client.streamChat;
      if (!retryStream && !plainStream) {
        // 注入的 client 未实现流式通道(仅测试 stub 场景),明确报错,不静默降级
        set({
          isStreaming: false,
          requestError: new ApiClientError("当前环境不支持流式对话。", {
            code: null,
            status: null,
          }),
        });
        return;
      }

      // D4-T3:守卫通过后创建本轮 controller 并挂到实例闭包,供
      // cancelStreaming abort;signal 透传给流式通道(StreamChatOptions
      // 已支持,abort 时静默返回)。
      const controller = new AbortController();
      activeStreamController = controller;

      // UX-20260807#1:乐观回显——与 sendMessage 同构,流式请求发起前
      // 先把用户消息追加进 messages(输入框立即清空后用户能看到自己
      // 说了什么)。run 正常结束后权威历史整体替换 messages；连接失败
      // 时保留乐观消息与已经收到的部分结果，
      // 乐观消息自然被覆盖,无需去重。
      const optimistic: Message = {
        agent: null,
        content: message,
        created_at: undefined,
        role: "user",
      };

      set((state) => ({
        isStreaming: true,
        degradedNotice: null,
        requestError: null,
        runError: null,
        streamingAgent: null,
        streamingMessage: null,
        // D2-T5:流式通道也记录上一条消息(与 sendMessage 同一语义)
        lastSentMessage: message,
        // events 在 run 开始时重置(与 sendMessage 一致):契约中事件是
        // 本轮增量,累积会让时间线跨 run 交错(review 修正)。
        events: [],
        messages: [...state.messages, optimistic],
      }));

      const dispatchContext: StreamDispatchContext = {
        activeSupervisorMessageId: null,
      };
      const dispatch = (event: StreamEvent) =>
        dispatchStreamEvent(set, get, sessionId, event, dispatchContext);

      try {
        if (retryStream) {
          await retryStream({
            sessionId,
            message,
            attachments,
            maxRetries: _STREAM_RETRY_LIMIT,
            onEvent: dispatch,
            signal: controller.signal,
          });
        } else if (plainStream) {
          // 旧 stub 兼容路径:无重试,失败行为与 D1-T1 一致(不降级)
          await plainStream({
            sessionId,
            message,
            attachments,
            onEvent: dispatch,
            signal: controller.signal,
          });
        }

        // 正常结束:把流式气泡并入消息列表,再拉一次权威历史覆盖
        // (与 sendMessage 的「POST 后拉全量」两段式保持一致)
        if (get().currentSessionId === sessionId) {
          set((state) => ({
            isStreaming: false,
            messages: state.streamingMessage
              ? [...state.messages, state.streamingMessage]
              : state.messages,
            streamingAgent: null,
            streamingMessage: null,
          }));
        }
        // 拉权威历史失败 ≠ 流式通道失败:只报错,绝不触发降级重发——
        // 消息已经送达,重发同一条消息会造成历史重复(review 修正)。
        try {
          await get().loadCurrentSessionMessages();
        } catch (historyError) {
          if (get().currentSessionId === sessionId) {
            set({ requestError: asApiClientError(historyError) });
          }
        }
        // UX-20260808#1:与 sendMessage 同步路径一致——消息落库后静默
        // 刷新会话列表,让后端补写的标题尽快替换侧栏的 session_id。
        void get().refreshSessions({ quiet: true });
      } catch (error) {
        // 此处只处理流式通道(streamChatWithRetry / streamChat)抛出的错误。
        // D4-T3 review 修正:用户点击「停止生成」(abort)恰逢后端错误
        // 响应时,stream-client 的 catch 先判 ApiClientError 再判
        // signal.aborted,错误可能透传到这里——若此时误走降级分支会
        // sendMessage 重发同一条已送达消息。abort 短路:取消路径直接
        // 走正常收尾(内容保留、不降级、不重发)。
        if (controller.signal.aborted) {
          if (get().currentSessionId === sessionId) {
            set({ isStreaming: false });
          }
          return;
        }
        if (get().currentSessionId === sessionId) {
          // 流式请求是否已经送达服务端在断线后不可判定，因此绝不能
          // 自动切换 POST /chat 重发同一消息。保留乐观用户消息与已收到
          // 的部分回答，让用户看得见发生了什么；仅标记连接失败。
          set({
            degradedNotice: null,
            isStreaming: false,
            requestError: asApiClientError(error),
            streamingAgent: null,
          });
        }
      } finally {
        // D4-T3:清理 controller 引用(按引用比对,只清自己的;流结束后
        // cancelStreaming 不再影响后续新流)。放在会话守卫外:切会话的
        // 旧流收尾也要释放引用,否则泄漏。
        if (activeStreamController === controller) {
          activeStreamController = null;
        }
        // 兜底:任何路径都不让 isStreaming 悬挂;但只在会话未切换时
        // 复位——否则流 A 收尾会误清「切会话后开始的流 B」的
        // isStreaming(review 修正,后果是新流提前解锁输入)。
        if (get().currentSessionId === sessionId) {
          set({ isStreaming: false });
        }
      }
    },
  }));
}

export const useChatStore = createChatStore();
