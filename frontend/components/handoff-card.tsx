"use client";

// D2-T3:待审批手递交接卡片(纯展示)。
// 展示后端 PendingHandoff 的审批信息(目标 Agent、任务内容、计划步骤),
// 提供确认/拒绝两个操作。所有数据与回调由父组件(ConversationPanel)从
// store 订阅后以 props 传入,自身不订阅 store,便于 SSR 渲染与组件测试。
// 对 null(pending 为空)直接不渲染;对决策中(isDeciding)与错误文案健壮。
import { LoaderCircle } from "lucide-react";

import { AgentBadge } from "@/components/agent-badge";
import { Button } from "@/components/ui/button";
import type { components } from "@/contracts/api.generated";

type PendingHandoff = components["schemas"]["PendingHandoff"];

export type HandoffDecisionAction = "confirm" | "reject";

export type HandoffCardProps = {
  // 决策相关错误文案(store requestError 映射后传入,仅审批相关错误码)
  errorMessage?: string | null;
  isDeciding: boolean;
  onDecide: (action: HandoffDecisionAction) => void;
  pending: PendingHandoff | null;
};

export function HandoffCard({
  errorMessage,
  isDeciding,
  onDecide,
  pending,
}: HandoffCardProps) {
  if (!pending) {
    return null;
  }

  const { request } = pending;

  return (
    <section
      className="overflow-hidden rounded-lg border border-border bg-card"
      data-slot="handoff-card"
    >
      <header className="flex items-center justify-between border-b border-border px-4 py-2">
        <h3 className="text-caption font-medium text-foreground">等待审批</h3>
        {/* target_agent 是 WorkerAgentRole(AgentRole 的子集),直接复用徽标 */}
        <AgentBadge agent={request.target_agent} />
      </header>

      <div className="space-y-2 px-4 py-3">
        {/* 任务内容:原样展示,保留换行 */}
        <p className="whitespace-pre-wrap text-body text-foreground">{request.task_content}</p>
        {/* plan_step_sequence 非 null 时标注所属计划步骤 */}
        {request.plan_step_sequence != null ? (
          <p className="text-caption text-muted-foreground">
            步骤 #{request.plan_step_sequence}
          </p>
        ) : null}
      </div>

      {/* 决策错误:仅审批相关错误码会经 errorMessage 传入 */}
      {errorMessage ? (
        <p className="px-4 pb-2 text-caption text-destructive" role="alert">
          {errorMessage}
        </p>
      ) : null}

      <footer className="flex items-center gap-2 border-t border-border px-4 py-3">
        <Button
          data-slot="handoff-reject"
          disabled={isDeciding}
          type="button"
          variant="outline"
          onClick={() => onDecide("reject")}
        >
          拒绝
        </Button>
        <Button
          data-slot="handoff-confirm"
          disabled={isDeciding}
          type="button"
          onClick={() => onDecide("confirm")}
        >
          确认
        </Button>
        {isDeciding ? (
          <span
            className="ml-auto flex items-center gap-1.5 text-caption text-muted-foreground"
            data-slot="handoff-deciding"
          >
            <LoaderCircle aria-hidden className="size-3.5 animate-spin" />
            处理中…
          </span>
        ) : null}
      </footer>
    </section>
  );
}
