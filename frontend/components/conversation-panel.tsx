"use client";

import { LoaderCircle } from "lucide-react";
import { useEffect, useRef } from "react";

import { AgentBadge } from "@/components/agent-badge";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import { ChatInput } from "@/components/chat-input";
import { CollaborationPanel } from "@/components/collaboration-panel";
import { HandoffCard } from "@/components/handoff-card";
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
  isStreaming: boolean;
  messages: Message[];
  runError: NonNullable<ChatResponse["run_error"]> | null;
  streamingAgent: AgentRole | null;
  streamingMessage: Message | null;
};

export function ConversationContent({
  isSending,
  isStreaming,
  messages,
  runError,
  streamingAgent,
  streamingMessage,
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

      {/* 流式气泡:isStreaming 期间渲染,或异常中断后保留已收到内容时继续展示 */}
      {isStreaming || streamingMessage ? (
        <article
          className="flex justify-start"
          data-message-role="assistant"
          data-slot="streaming-message"
        >
          <div className="max-w-[80%] rounded-lg border border-border bg-card px-4 py-3 text-body text-foreground">
            <AssistantBadge agent={streamingAgent} />
            <div className="mt-2">
              <AssistantMarkdown content={streamingMessage?.content ?? ""} />
            </div>
            {isStreaming ? (
              <div className="mt-2 flex items-center gap-2 text-caption text-muted-foreground">
                <LoaderCircle aria-hidden className="size-4 animate-spin" />
                正在生成…
              </div>
            ) : null}
          </div>
        </article>
      ) : null}

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
  // D2-T2:协作过程面板所需数据(计划、结果、事件、活跃 Agent)由这里订阅后传入
  const currentAgent = useChatStore((state) => state.currentAgent);
  // D2-T3:审批卡片数据(待审批项、决策中标记、决策错误、决策动作)
  const decideHandoff = useChatStore((state) => state.decideHandoff);
  const events = useChatStore((state) => state.events);
  const isDecidingHandoff = useChatStore((state) => state.isDecidingHandoff);
  const isSending = useChatStore((state) => state.isSending);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const messages = useChatStore((state) => state.messages);
  const pendingHandoff = useChatStore((state) => state.pendingHandoff);
  const requestError = useChatStore((state) => state.requestError);
  const runError = useChatStore((state) => state.runError);
  const streamingAgent = useChatStore((state) => state.streamingAgent);
  const streamingMessage = useChatStore((state) => state.streamingMessage);
  const taskPlan = useChatStore((state) => state.taskPlan);
  const taskResults = useChatStore((state) => state.taskResults);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [
    currentAgent,
    events,
    isDecidingHandoff,
    isSending,
    isStreaming,
    messages,
    pendingHandoff,
    runError,
    streamingAgent,
    streamingMessage,
    taskPlan,
    taskResults,
  ]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto" data-slot="message-list">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-8 py-6">
          <ConversationContent
            isSending={isSending}
            isStreaming={isStreaming}
            messages={messages}
            runError={runError ?? null}
            streamingAgent={streamingAgent}
            streamingMessage={streamingMessage}
          />
          {/* D2-T2:协作过程面板——消息流与输入区之间,展示计划与事件 */}
          <CollaborationPanel
            currentAgent={currentAgent}
            events={events}
            taskPlan={taskPlan}
            taskResults={taskResults}
          />
          {/* D2-T3:审批卡片——协作面板之后、输入区之前;错误文案只映射
              审批相关错误码,其它 requestError 仍由侧栏等现有路径处理。
              handoff_not_pending 不在此映射:store 收到该码会清除并刷新
              pending,卡片随之消失,错误行永远不会显示(死分支已删,
              review nit)。 */}
          <HandoffCard
            errorMessage={
              requestError?.code === "session_busy"
                ? requestError.message
                : null
            }
            isDeciding={isDecidingHandoff}
            onDecide={(action, modifications) =>
              void decideHandoff(action, modifications)
            }
            pending={pendingHandoff}
          />
          <div data-slot="conversation-end" ref={endRef} />
        </div>
      </div>
      <div className="border-t border-border px-8 py-4" data-slot="chat-input-area">
        <div className="mx-auto w-full max-w-3xl">
          <ChatInput />
        </div>
      </div>
    </div>
  );
}
