"use client";

import { create } from "zustand";

import {
  ApiClientError,
  apiClient,
  type ApiClient,
  type ChatResponse,
  type Message,
  type PendingHandoffResponse,
  type Session,
} from "../lib/api-client";
import type { components } from "../contracts/api.generated";

type ChatStoreClient = Pick<
  ApiClient,
  "archiveSession" | "createSession" | "getSessionMessages" | "listSessions" | "sendChat"
> & {
  // 可选:既有测试注入的 stub 未实现流式通道,正式 apiClient 一定实现
  streamChat?: ApiClient["streamChat"];
  // D1-T3:断线重连通道(指数退避重试 + fromSequence 续传)。store 优先
  // 使用它;未注入(旧测试 stub)时退回底层 streamChat,失败不降级
  // (保持 D1-T1 行为)。
  streamChatWithRetry?: ApiClient["streamChatWithRetry"];
};
type PendingHandoff = PendingHandoffResponse["pending_handoff"];
type RunError = ChatResponse["run_error"];
type RunEvent = components["schemas"]["RunEvent"];
type StreamEvent = components["schemas"]["StreamEvent"];
type AgentRole = components["schemas"]["AgentRole"];

// D1-T3:流式通道重试上限(重试次数,不含首次尝试),传给
// streamChatWithRetry 的 maxRetries;耗尽后降级到同步通道。
const _STREAM_RETRY_LIMIT = 3;

export type ChatStore = {
  archiveSession(sessionId: string): Promise<void>;
  clearConversationState(): void;
  clearRequestError(): void;
  createSession(): Promise<Session | null>;
  currentSessionId: string | null;
  degradedNotice: string | null;
  events: (RunEvent | StreamEvent)[];
  isLoadingMessages: boolean;
  isLoadingSessions: boolean;
  isSending: boolean;
  isStreaming: boolean;
  loadCurrentSessionMessages(): Promise<void>;
  messages: Message[];
  pendingHandoff: PendingHandoff | null;
  refreshSessions(): Promise<void>;
  requestError: ApiClientError | null;
  runError: RunError | null;
  selectSession(sessionId: string | null): void;
  sendMessage(message: string): Promise<void>;
  sessions: Session[];
  streamSendMessage(message: string): Promise<void>;
  streamingAgent: AgentRole | null;
  streamingMessage: Message | null;
};

function emptyConversationState() {
  return {
    degradedNotice: null as string | null,
    events: [] as (RunEvent | StreamEvent)[],
    isLoadingMessages: false,
    isSending: false,
    isStreaming: false,
    messages: [] as Message[],
    pendingHandoff: null,
    runError: null,
    streamingAgent: null,
    streamingMessage: null,
  };
}

function asApiClientError(error: unknown): ApiClientError {
  if (error instanceof ApiClientError) {
    return error;
  }

  return new ApiClientError("请求失败，请稍后重试。", { code: null, status: null });
}

export function createChatStore(client: ChatStoreClient = apiClient) {
  return create<ChatStore>()((set, get) => ({
    ...emptyConversationState(),
    currentSessionId: null,
    isLoadingSessions: false,
    requestError: null,
    sessions: [],
    archiveSession: async (sessionId) => {
      set({ requestError: null });
      try {
        await client.archiveSession(sessionId);
        set((state) => ({
          ...(state.currentSessionId === sessionId ? emptyConversationState() : {}),
          currentSessionId:
            state.currentSessionId === sessionId ? null : state.currentSessionId,
          sessions: state.sessions.filter((session) => session.session_id !== sessionId),
        }));
      } catch (error) {
        set({ requestError: asApiClientError(error) });
      }
    },
    clearConversationState: () => set(emptyConversationState()),
    clearRequestError: () => set({ requestError: null }),
    createSession: async () => {
      set({ requestError: null });
      try {
        const session = await client.createSession();
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
        const messages = await client.getSessionMessages(sessionId);
        if (get().currentSessionId === sessionId) {
          set({ isLoadingMessages: false, messages });
        }
      } catch (error) {
        if (get().currentSessionId === sessionId) {
          set({ isLoadingMessages: false, requestError: asApiClientError(error) });
        }
      }
    },
    refreshSessions: async () => {
      set({ isLoadingSessions: true, requestError: null });
      try {
        const sessions = await client.listSessions();
        set({ isLoadingSessions: false, sessions });
      } catch (error) {
        set({ isLoadingSessions: false, requestError: asApiClientError(error) });
      }
    },
    selectSession: (sessionId) =>
      set({
        ...emptyConversationState(),
        currentSessionId: sessionId,
        requestError: null,
      }),
    sendMessage: async (message) => {
      const sessionId = get().currentSessionId;
      if (!sessionId) {
        set({
          requestError: new ApiClientError("请先选择会话。", { code: null, status: null }),
        });
        return;
      }

      set({ isSending: true, requestError: null });
      try {
        const response = await client.sendChat({ message, session_id: sessionId });
        const messages = await client.getSessionMessages(sessionId);
        if (get().currentSessionId === sessionId) {
          set((state) => ({
            events: [...state.events, ...(response.events ?? [])],
            isSending: false,
            messages,
            pendingHandoff: response.pending_handoff ?? null,
            runError: response.run_error ?? null,
          }));
        }
      } catch (error) {
        if (get().currentSessionId === sessionId) {
          set({ isSending: false, requestError: asApiClientError(error) });
        }
      }
    },
    streamSendMessage: async (message) => {
      const sessionId = get().currentSessionId;
      if (!sessionId) {
        set({
          requestError: new ApiClientError("请先选择会话。", { code: null, status: null }),
        });
        return;
      }

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

      set({
        isStreaming: true,
        requestError: null,
        runError: null,
        streamingAgent: null,
        streamingMessage: null,
      });

      // 事件分发与 sendMessage 的 response.events 同一列表(会话守卫一致)
      const dispatch = (event: StreamEvent) => {
        // 旧会话的流事件不回写新会话状态(与 sendMessage 的会话守卫一致)
        if (get().currentSessionId !== sessionId) {
          return;
        }
        switch (event.event_type) {
          case "thinking":
            // thinking 的 content 只是占位文本,不进消息体,仅更新当前 agent
            set({ streamingAgent: event.agent ?? null });
            break;
          case "tool_call":
          case "tool_result":
            // 摘要事件追加进 events(与 sendMessage 的 response.events 同一列表)
            set((state) => ({ events: [...state.events, event] }));
            break;
          case "agent_switch":
            set({ streamingAgent: event.agent ?? null });
            break;
          case "message_end": {
            const streamed: Message = event.message ?? {
              agent: event.agent ?? undefined,
              content: event.content ?? "",
              created_at: undefined,
              role: "assistant",
            };
            set({ streamingMessage: streamed });
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
            // done 无需处理,await 返回后统一收尾
            break;
        }
      };

      try {
        if (retryStream) {
          await retryStream({
            sessionId,
            message,
            maxRetries: _STREAM_RETRY_LIMIT,
            onEvent: dispatch,
          });
        } else if (plainStream) {
          // 旧 stub 兼容路径:无重试,失败行为与 D1-T1 一致(不降级)
          await plainStream({ sessionId, message, onEvent: dispatch });
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
      } catch (error) {
        // 此处只处理流式通道(streamChatWithRetry / streamChat)抛出的错误
        if (get().currentSessionId === sessionId) {
          if (retryStream) {
            // D1-T3 降级:重试通道耗尽(重试 + 续传均失败)后走同步通道
            // 保证消息一致。sendMessage 内部「POST + 拉全量」以历史为
            // 权威,流式残留先并入消息列表再拉全量,不闪失。
            set({ isStreaming: false, requestError: asApiClientError(error) });
            set({
              degradedNotice: "网络不稳定,已切换到同步通道,消息可能缺少过程事件。",
            });
            const streamed = get().streamingMessage;
            if (streamed) {
              set((state) => ({
                messages: [...state.messages, streamed],
                streamingMessage: null,
              }));
            }
            try {
              await get().sendMessage(message);
            } catch {
              // sendMessage 内部已处理错误(requestError / 会话守卫),无需再处理
            }
          } else {
            // 旧通道失败:保留已流式收到的内容(streamingMessage 不清空),
            // 仅标记失败(D1-T1 行为)
            set({ isStreaming: false, requestError: asApiClientError(error) });
          }
        }
      } finally {
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
