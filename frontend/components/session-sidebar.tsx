"use client";

import { Archive, Plus } from "lucide-react";
import { useEffect } from "react";

import { Button } from "@/components/ui/button";
import { useChatStore } from "@/stores/chat-store";

export function SessionSidebar() {
  const archiveSession = useChatStore((state) => state.archiveSession);
  const createSession = useChatStore((state) => state.createSession);
  const currentSessionId = useChatStore((state) => state.currentSessionId);
  const isLoadingSessions = useChatStore((state) => state.isLoadingSessions);
  const loadCurrentSessionMessages = useChatStore(
    (state) => state.loadCurrentSessionMessages,
  );
  const requestError = useChatStore((state) => state.requestError);
  const refreshSessions = useChatStore((state) => state.refreshSessions);
  const selectSession = useChatStore((state) => state.selectSession);
  const sessions = useChatStore((state) => state.sessions);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  return (
    <aside
      className="flex min-h-screen w-72 flex-col border-r border-border bg-card"
      data-slot="session-sidebar"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-4">
        <div>
          <p className="text-caption font-medium text-muted-foreground">协作式 Agent</p>
          <h1 className="text-body font-semibold text-foreground">会话</h1>
        </div>
        <Button
          aria-label="新建会话"
          className="gap-2"
          onClick={() => void createSession()}
          size="sm"
          type="button"
        >
          <Plus aria-hidden className="size-4" />
          新建会话
        </Button>
      </div>

      <div className="flex-1 overflow-y-auto p-2">
        {isLoadingSessions ? (
          <p className="px-2 py-3 text-caption text-muted-foreground">正在加载会话…</p>
        ) : null}

        {!isLoadingSessions && sessions.length === 0 ? (
          <p className="px-2 py-3 text-caption text-muted-foreground">暂无会话</p>
        ) : null}

        {sessions.map((session) => {
          const selected = session.session_id === currentSessionId;

          return (
            <div
              className={
                selected
                  ? "group mb-1 flex items-center rounded-md bg-muted px-3 py-2"
                  : "group mb-1 flex items-center rounded-md px-3 py-2 hover:bg-muted/60"
              }
              key={session.session_id}
            >
              <button
                className="min-w-0 flex-1 truncate text-left text-caption font-medium text-foreground"
                onClick={() => {
                  selectSession(session.session_id);
                  void loadCurrentSessionMessages();
                }}
                type="button"
              >
                {session.session_id}
              </button>
              <button
                aria-label={`归档会话 ${session.session_id}`}
                className="ml-2 inline-flex size-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-background hover:text-foreground"
                onClick={() => void archiveSession(session.session_id)}
                type="button"
              >
                <Archive aria-hidden className="size-4" />
              </button>
            </div>
          );
        })}
      </div>

      {requestError ? (
        <p className="border-t border-border px-4 py-3 text-caption text-destructive" role="alert">
          {requestError.message}
        </p>
      ) : null}
    </aside>
  );
}
