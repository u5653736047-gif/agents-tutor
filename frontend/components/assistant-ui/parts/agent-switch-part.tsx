"use client";

// assistant-ui 接入(T4 基础版,T8 增强):agent_switch data part 渲染器。
// 多智能体接力的可视分隔:细分隔线 + 目标角色徽章。连续重复角色已由
// 转换器去重(message-converter.ts 的 lastSwitchAgent 语义),本组件纯展示。

import type { DataMessagePartProps } from "@assistant-ui/react";

import { AgentBadge } from "@/components/agent-badge";
import type { AgentRole } from "@/lib/agent-roles";

type AgentSwitchData = { agent?: unknown };

export function AgentSwitchPart({ data }: DataMessagePartProps) {
  const agent = (data as AgentSwitchData | undefined)?.agent;
  if (typeof agent !== "string") {
    return null;
  }
  return (
    <div
      className="flex items-center gap-3 py-1"
      data-slot="agent-switch"
      role="separator"
    >
      <span aria-hidden className="h-px flex-1 bg-border" />
      <AgentBadge agent={agent as AgentRole} />
      <span aria-hidden className="h-px flex-1 bg-border" />
    </div>
  );
}
