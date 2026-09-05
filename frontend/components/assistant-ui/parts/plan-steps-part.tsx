"use client";

// assistant-ui 接入(T8):plan-steps data part 渲染器——Supervisor 有序任务
// 计划的步骤条。视觉复刻旧面板 PlanSteps(collaboration-panel.tsx L86-156):
// 状态徽章 + 步骤列表(current_step_index ring 高亮,结果 ✓/✗),全部走
// 语义 token(planStatusPresentation 的 30/10/100 配色公式)。

import type { DataMessagePartProps } from "@assistant-ui/react";

import { AgentBadge } from "@/components/agent-badge";
import type { components } from "@/contracts/api.generated";
import { cn } from "@/lib/utils";

type TaskPlan = components["schemas"]["TaskPlan"];
type TaskResult = components["schemas"]["TaskResult"];

type PlanStepsData = { plan?: unknown; results?: unknown };

// 计划状态的展示文案与配色(与旧面板 planStatusPresentation 一致)
const planStatusPresentation: Record<
  TaskPlan["status"],
  { label: string; className: string }
> = {
  active: { label: "进行中", className: "border-primary/30 bg-primary/10 text-primary" },
  completed: {
    label: "已完成",
    className: "border-success/30 bg-success/10 text-success",
  },
  cancelled: { label: "已取消", className: "border-border bg-muted text-muted-foreground" },
  failed: { label: "失败", className: "border-destructive/30 bg-destructive/10 text-destructive" },
};

// 长文本摘要:与旧面板 summarize 同口径(40 字符截断)
function summarize(text: string | null | undefined, maxLength = 40): string {
  if (!text) {
    return "";
  }
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

// 宽容读取:非法 plan 结构(缺 steps 数组)按无计划处理,零渲染
function isTaskPlan(value: unknown): value is TaskPlan {
  return (
    typeof value === "object" &&
    value !== null &&
    Array.isArray((value as TaskPlan).steps) &&
    typeof (value as TaskPlan).current_step_index === "number" &&
    typeof (value as TaskPlan).status === "string"
  );
}

function isTaskResultArray(value: unknown): value is TaskResult[] {
  return Array.isArray(value);
}

export function PlanStepsPart({ data }: DataMessagePartProps) {
  const payload = data as PlanStepsData | undefined;
  if (!isTaskPlan(payload?.plan)) {
    return null;
  }
  const plan = payload.plan;
  const results = isTaskResultArray(payload?.results) ? payload.results : null;
  const status = planStatusPresentation[plan.status];

  return (
    <div
      className="rounded-md border border-border bg-muted/40 px-3 py-2"
      data-slot="plan-steps"
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-caption font-medium",
            status.className,
          )}
        >
          {status.label}
        </span>
        <p className="text-caption text-muted-foreground">
          计划共 {plan.steps.length} 步
        </p>
      </div>

      <ol className="mt-2 flex flex-wrap gap-2">
        {plan.steps.map((step, index) => {
          // 当前正在执行的步骤由 current_step_index 指定,加 ring 高亮
          const isCurrent = index === plan.current_step_index;
          // 按步骤序号匹配执行结果(success 打勾,失败打叉)
          const result =
            results?.find((item) => item.step_sequence === step.sequence) ?? null;

          return (
            <li
              className={cn(
                "min-w-0 rounded-md border px-2 py-1.5 text-caption",
                isCurrent ? "border-primary ring-1 ring-primary" : "border-border",
              )}
              data-current={isCurrent ? "true" : undefined}
              data-result={result ? (result.success ? "success" : "failed") : undefined}
              key={step.sequence}
            >
              <div className="flex items-center gap-1.5">
                <span className="text-muted-foreground">#{step.sequence}</span>
                <span className="truncate text-foreground">{step.description}</span>
                <AgentBadge agent={step.target_agent} />
              </div>

              {result ? (
                <div className="mt-1 flex items-center gap-1">
                  {result.success ? (
                    <span aria-label="成功" className="text-success">
                      ✓
                    </span>
                  ) : (
                    <span aria-label="失败" className="text-destructive">
                      ✗
                    </span>
                  )}
                  <span className="truncate text-muted-foreground">
                    {result.success
                      ? summarize(result.output) || "完成"
                      : (result.error_code ?? "执行失败")}
                  </span>
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
