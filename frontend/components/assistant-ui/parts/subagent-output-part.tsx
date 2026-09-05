"use client";

// assistant-ui 接入(T4 基础版,T8 增强):子代理阶段输出卡。
// 非 supervisor 的 message_delta(Worker 阶段性输出)以内联卡片呈现,
// 与最终回答文本刻意分开(转换器语义:避免与 message_end 权威全文重复)。

import type { DataMessagePartProps } from "@assistant-ui/react";

import { AgentBadge } from "@/components/agent-badge";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import type { AgentRole } from "@/lib/agent-roles";

type SubagentOutputData = { agent?: unknown; content?: unknown };

export function SubagentOutputPart({ data }: DataMessagePartProps) {
  const payload = data as SubagentOutputData | undefined;
  const content = typeof payload?.content === "string" ? payload.content : "";
  if (!content.trim()) {
    return null;
  }
  const agent = payload?.agent;
  return (
    <div
      className="rounded-md border border-border bg-muted/40 px-3 py-2"
      data-slot="subagent-message"
    >
      {typeof agent === "string" ? (
        <AgentBadge agent={agent as AgentRole} />
      ) : null}
      <div className="mt-1 text-caption">
        <AssistantMarkdown content={content} />
      </div>
    </div>
  );
}
