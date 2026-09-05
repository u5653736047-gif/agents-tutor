"use client";

// assistant-ui 接入(T8):thinking data part 渲染器——Agent 阶段提示行
// (「正在分析问题并规划协作」等占位文案)。视觉复刻旧面板 EventTimeline
// 的 thinking 行(collaboration-panel.tsx L209-224:角色徽章 + 单行文本)。

import type { DataMessagePartProps } from "@assistant-ui/react";

import { AgentBadge } from "@/components/agent-badge";
import type { AgentRole } from "@/lib/agent-roles";

type ThinkingData = { agent?: unknown; content?: unknown };

function isAgentRole(value: unknown): value is AgentRole {
  return (
    value === "supervisor" ||
    value === "teaching_assistant" ||
    value === "learning_assistant" ||
    value === "evaluator"
  );
}

export function ThinkingPart({ data }: DataMessagePartProps) {
  const payload = data as ThinkingData | undefined;
  const content = typeof payload?.content === "string" ? payload.content : "";
  if (!content.trim()) {
    return null;
  }
  const agent = isAgentRole(payload?.agent) ? payload.agent : null;
  return (
    <p
      className="flex items-center gap-2 text-caption text-muted-foreground"
      data-slot="thinking-row"
    >
      {agent ? <AgentBadge agent={agent} /> : null}
      <span className="truncate">{content}</span>
    </p>
  );
}
