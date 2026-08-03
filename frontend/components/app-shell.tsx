"use client";

import { CircleCheck, CircleX } from "lucide-react";

import { ConversationPanel } from "@/components/conversation-panel";
import { SessionSidebar } from "@/components/session-sidebar";
import { useChatStore } from "@/stores/chat-store";

type AppShellProps = {
  apiConnected: boolean;
};

export function AppShell({ apiConnected }: AppShellProps) {
  const currentSessionId = useChatStore((state) => state.currentSessionId);

  return (
    <main
      className="grid min-h-screen grid-cols-[18rem_minmax(0,1fr)] bg-background"
      data-layout="desktop-two-column"
      data-slot="app-shell"
    >
      <SessionSidebar />

      <section className="flex min-w-0 flex-col" data-slot="conversation-area">
        <header className="flex items-center justify-between border-b border-border px-8 py-4">
          <div>
            <p className="text-caption font-medium text-primary">阶段三 · 协作工作台</p>
            <h2 className="text-title font-semibold text-foreground">对话区</h2>
          </div>
          <div className="flex items-center gap-2 text-caption text-muted-foreground">
            {apiConnected ? (
              <CircleCheck aria-hidden className="size-4 text-emerald-600" />
            ) : (
              <CircleX aria-hidden className="size-4 text-destructive" />
            )}
            <span>{apiConnected ? "后端已连接" : "后端暂不可用"}</span>
          </div>
        </header>

        {currentSessionId ? (
          <ConversationPanel />
        ) : (
          <div className="flex flex-1 items-center justify-center p-8">
            <div className="max-w-md text-center">
              <p className="text-title font-semibold text-foreground">请选择或新建会话</p>
              <p className="mt-3 text-body text-muted-foreground">
                从左侧创建一个会话，或选择已有会话后开始对话。
              </p>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
