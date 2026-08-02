"use client";

import { LoaderCircle } from "lucide-react";
import { useEffect, useRef } from "react";

import { AgentBadge } from "@/components/agent-badge";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import type { AgentRole } from "@/lib/agent-roles";
import type { ChatResponse, Message } from "@/lib/api-client";
import { useChatStore } from "@/stores/chat-store";

function AssistantBadge({ agent }: { agent: AgentRole | null | undefined }) {
  if (agent) {
    return <AgentBadge agent={agent} />;
  }

  return (
    <span
      className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-caption font-medium text-muted-foreground"
      data-slot="assistant-badge-fallback"
    >
      助手
    </span>
  );
}

type ConversationContentProps = {
  isSending: boolean;
  messages: Message[];
  runError: NonNullable<ChatResponse["run_error"]> | null;
};

export function ConversationContent({
  isSending,
  messages,
  runError,
}: ConversationContentProps) {
  return (
    <>
      {messages.map((message, index) => {
        const isUser = message.role === "user";

        return (
          <article
            className={isUser ? "flex justify-end" : "flex justify-start"}
            data-message-role={message.role}
            key={message.created_at ?? `${message.role}-${index}`}
          >
            <div
              className={
                isUser
                  ? "max-w-[80%] rounded-lg bg-primary px-4 py-3 text-body text-primary-foreground"
                  : "max-w-[80%] rounded-lg border border-border bg-card px-4 py-3 text-body text-foreground"
              }
            >
              {!isUser ? <AssistantBadge agent={message.agent} /> : null}
              {isUser ? (
                <p className="whitespace-pre-wrap">{message.content}</p>
              ) : (
                <div className="mt-2">
                  <AssistantMarkdown content={message.content} />
                </div>
              )}
            </div>
          </article>
        );
      })}

      {runError ? (
        <p className="text-caption text-destructive" role="alert">
          本轮执行提示：{runError.message}
        </p>
      ) : null}

      {isSending ? (
        <div className="flex items-center gap-2 text-caption text-muted-foreground">
          <LoaderCircle aria-hidden className="size-4 animate-spin" />
          正在生成回答…
        </div>
      ) : null}
    </>
  );
}

export function ConversationPanel() {
  const isSending = useChatStore((state) => state.isSending);
  const messages = useChatStore((state) => state.messages);
  const runError = useChatStore((state) => state.runError);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [isSending, messages, runError]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto" data-slot="message-list">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-8 py-6">
        <ConversationContent
          isSending={isSending}
          messages={messages}
          runError={runError ?? null}
        />
        <div data-slot="conversation-end" ref={endRef} />
      </div>
    </div>
  );
}
