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
>;
type PendingHandoff = PendingHandoffResponse["pending_handoff"];
type RunError = ChatResponse["run_error"];
type RunEvent = components["schemas"]["RunEvent"];

export type ChatStore = {
  archiveSession(sessionId: string): Promise<void>;
  clearConversationState(): void;
  clearRequestError(): void;
  createSession(): Promise<Session | null>;
  currentSessionId: string | null;
  events: RunEvent[];
  isLoadingMessages: boolean;
  isLoadingSessions: boolean;
  isSending: boolean;
  loadCurrentSessionMessages(): Promise<void>;
  messages: Message[];
  pendingHandoff: PendingHandoff | null;
  refreshSessions(): Promise<void>;
  requestError: ApiClientError | null;
  runError: RunError | null;
  selectSession(sessionId: string | null): void;
  sendMessage(message: string): Promise<void>;
  sessions: Session[];
};

function emptyConversationState() {
  return {
    events: [] as RunEvent[],
    isLoadingMessages: false,
    isSending: false,
    messages: [] as Message[],
    pendingHandoff: null,
    runError: null,
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
  }));
}

export const useChatStore = createChatStore();
