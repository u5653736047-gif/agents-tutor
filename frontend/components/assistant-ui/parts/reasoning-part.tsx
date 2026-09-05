"use client";

// assistant-ui 接入(T5):reasoning part 渲染器——思维链完整展示。
// 行为语义复刻 collaboration-panel.tsx L226-253 的 <details open={流式中}>:
// 流式期间自动展开实时可见,结束后默认折叠;多智能体交错时按转换器分段的
// part 逐段呈现,标题行展示产出角色徽章(经 providerMetadata 应用命名空间
// 透传,见 message-converter.ts 的 PROVIDER_METADATA_NS);data-slot 锚点
// 与 e2e 既有断言(reasoning-block)对齐。

import type { ReasoningMessagePartProps } from "@assistant-ui/react";
import { LoaderCircle } from "lucide-react";

import { AgentBadge } from "@/components/agent-badge";
import { PROVIDER_METADATA_NS } from "@/lib/assistant/message-converter";
import type { AgentRole } from "@/lib/agent-roles";

// 宽容读取:providerMetadata 是开放平台元数据,非法结构按无角色处理
function agentFromMetadata(value: unknown): AgentRole | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const agent = (value as Record<string, unknown>)[PROVIDER_METADATA_NS];
  if (typeof agent !== "object" || agent === null) {
    return null;
  }
  const role = (agent as Record<string, unknown>).agent;
  return role === "supervisor" ||
    role === "teaching_assistant" ||
    role === "learning_assistant" ||
    role === "evaluator"
    ? role
    : null;
}

export function ReasoningPart({
  text,
  status,
  providerMetadata,
}: ReasoningMessagePartProps) {
  const running = status.type === "running";
  const agent = agentFromMetadata(providerMetadata);
  return (
    <details
      className="rounded-md border border-border bg-muted/40 px-3 py-2"
      data-slot="reasoning-block"
      open={running}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 text-caption font-medium text-muted-foreground">
        {running ? (
          <LoaderCircle aria-hidden className="size-3.5 animate-spin" />
        ) : null}
        {agent ? <AgentBadge agent={agent} /> : null}
        <span>思维链</span>
      </summary>
      <div className="mt-2 whitespace-pre-wrap text-caption text-muted-foreground">
        {text}
      </div>
    </details>
  );
}
