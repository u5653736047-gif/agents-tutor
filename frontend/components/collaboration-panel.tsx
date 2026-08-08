"use client";

// D2-T2:协作过程面板。
// 展示一次运行的两块信息:
//   1. 计划步骤条(task_plan):后端规划的步骤,叠加 task_results 的执行结果;
//   2. 事件时间线(events):thinking / tool_call / tool_result / agent_switch 摘要。
// 纯展示组件:所有数据由父组件(ConversationPanel)从 store 订阅后以 props 传入,
// 自身不订阅 store,便于 SSR 渲染与组件测试。对 null / 空数组健壮。
import { ChevronDown, ChevronUp } from "lucide-react";
import { useState } from "react";

import { AgentBadge } from "@/components/agent-badge";
import type { components } from "@/contracts/api.generated";
import type { AgentRole } from "@/lib/agent-roles";
import { cn } from "@/lib/utils";

// 与 chat-store 一致,直接从生成契约取类型,保持单一数据源
type RunEvent = components["schemas"]["RunEvent"];
type StreamEvent = components["schemas"]["StreamEvent"];
type TaskPlan = components["schemas"]["TaskPlan"];
type TaskResult = components["schemas"]["TaskResult"];

export type CollaborationPanelProps = {
  events: readonly (RunEvent | StreamEvent)[];
  taskPlan: TaskPlan | null;
  taskResults: TaskResult[] | null;
  currentAgent: AgentRole | null;
};

// 计划状态的展示文案与配色(与 AgentBadge 的 role-* 配色风格一致)
const planStatusPresentation: Record<
  TaskPlan["status"],
  { label: string; className: string }
> = {
  active: { label: "进行中", className: "border-primary/30 bg-primary/10 text-primary" },
  completed: {
    label: "已完成",
    // UX-20260807#4:成功色走语义 success token(替代 emerald 硬编码,
    // 两模式自动适配),与角色徽章同一「30/10/100 配色公式」。
    className: "border-success/30 bg-success/10 text-success",
  },
  cancelled: { label: "已取消", className: "border-border bg-muted text-muted-foreground" },
  failed: { label: "失败", className: "border-destructive/30 bg-destructive/10 text-destructive" },
};

// content 字段只在 StreamEvent 上存在(thinking 的占位文本),
// 用 in 操作符收窄联合类型后安全读取,RunEvent 返回 null。
function eventContent(event: RunEvent | StreamEvent): string | null {
  return "content" in event ? (event.content ?? null) : null;
}

// 活跃 Agent 兜底:取最后一条 agent_switch 的目标 Agent(按 sequence 排序)。
// 与 store 流式路径的 currentAgent 更新逻辑保持一致。
function lastSwitchAgent(events: readonly (RunEvent | StreamEvent)[]): AgentRole | null {
  const switches = events
    .filter((event) => event.event_type === "agent_switch")
    .sort((a, b) => a.sequence - b.sequence);
  return switches[switches.length - 1]?.agent ?? null;
}

// 长文本摘要:计划步骤的 output 可能很长,截断展示
function summarize(text: string | null | undefined, maxLength = 40): string {
  if (!text) {
    return "";
  }
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text;
}

// 计划步骤条:横向步骤列表,current_step_index 高亮,结果打勾/打叉
function PlanSteps({
  taskPlan,
  taskResults,
}: {
  taskPlan: TaskPlan;
  taskResults: TaskResult[] | null;
}) {
  const status = planStatusPresentation[taskPlan.status];

  return (
    <div className="border-b border-border px-4 py-3" data-slot="plan-steps">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "rounded-full border px-2 py-0.5 text-caption font-medium",
            status.className,
          )}
        >
          {status.label}
        </span>
        <p className="text-caption text-muted-foreground">计划共 {taskPlan.steps.length} 步</p>
      </div>

      <ol className="mt-2 flex flex-wrap gap-2">
        {taskPlan.steps.map((step, index) => {
          // 当前正在执行的步骤由 current_step_index 指定,加 ring 高亮
          const isCurrent = index === taskPlan.current_step_index;
          // 按步骤序号匹配执行结果(success 打勾,失败打叉并显示 error_code/output)
          const result = taskResults?.find((item) => item.step_sequence === step.sequence) ?? null;

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
                      : result.error_code ?? "执行失败"}
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

// 事件时间线:按 sequence 升序渲染,工具行可展开看详情
function EventTimeline({
  events,
  activeAgent,
}: {
  events: readonly (RunEvent | StreamEvent)[];
  activeAgent: AgentRole | null;
}) {
  // 传入顺序不保证有序,先按 sequence 升序排好再渲染
  const sorted = [...events].sort((a, b) => a.sequence - b.sequence);
  // 工具行展开状态:记录展开行在排序数组中的索引(而非 sequence)——
  // sequence 每轮 run 从 1 起,跨 run 复用会撞号导致旧行意外展开
  // (review 修正);索引在当次渲染的排序数组中稳定。
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  return (
    <ol className="space-y-1 px-4 py-3" data-slot="event-timeline">
      {sorted.map((event, sortedIndex) => {
        // 与当前活跃 Agent 相同的事件条目加高亮(加粗 + 前景色)
        const isActive = event.agent != null && event.agent === activeAgent;
        const activeProps = {
          "data-active": isActive ? "true" : undefined,
        };

        switch (event.event_type) {
          case "thinking": {
            // thinking:Agent 徽标 + 占位文本(后端只发占位内容,直接展示)
            return (
              <li
                className={cn(
                  "flex items-center gap-2 text-caption",
                  isActive ? "font-medium text-foreground" : "text-muted-foreground",
                )}
                key={`thinking-${event.sequence}`}
                {...activeProps}
              >
                {event.agent ? <AgentBadge agent={event.agent} /> : null}
                <span className="truncate">{eventContent(event) ?? "正在思考…"}</span>
              </li>
            );
          }
          case "tool_call":
          case "tool_result": {
            // 工具行:只含工具名与成功与否等摘要,绝无参数/结果正文(安全红线)。
            // 点击展开可看耗时与所属计划步骤;success 为 null 时不显示状态。
            const toolName = event.tool_name ?? "未知工具";
            const succeeded = event.success;
            const isExpanded = expandedIndex === sortedIndex;

            return (
              <li key={`${event.event_type}-${event.sequence}`}>
                <button
                  aria-expanded={isExpanded}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-caption",
                    isActive ? "font-medium text-foreground" : "text-muted-foreground",
                  )}
                  data-slot="tool-row"
                  type="button"
                  onClick={() =>
                    setExpandedIndex(isExpanded ? null : sortedIndex)
                  }
                  {...activeProps}
                >
                  <span aria-hidden>
                    {event.event_type === "tool_call"
                      ? "🔧"
                      : succeeded === true
                        ? "✅"
                        : succeeded === false
                          ? "❌"
                          : "🔧"}
                  </span>
                  <span className="truncate">{toolName}</span>
                  <span className="ml-auto shrink-0">{isExpanded ? "收起" : "详情"}</span>
                </button>

                {isExpanded ? (
                  <div className="ml-2 border-l border-border pl-3 text-caption text-muted-foreground">
                    {event.plan_step_sequence != null ? (
                      <p>所属计划步骤:{event.plan_step_sequence}</p>
                    ) : null}
                    {event.duration_ms != null ? <p>耗时:{event.duration_ms}ms</p> : null}
                    {event.event_type === "tool_result" && succeeded != null ? (
                      <p>{succeeded ? "执行成功" : "执行失败"}</p>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          }
          case "agent_switch": {
            // Agent 切换:轻量提示行(→ 目标 Agent)
            return (
              <li
                className={cn(
                  "flex items-center gap-2 text-caption",
                  isActive ? "font-medium text-foreground" : "text-muted-foreground",
                )}
                key={`agent_switch-${event.sequence}`}
                {...activeProps}
              >
                <span aria-hidden>→</span>
                {event.agent ? <AgentBadge agent={event.agent} /> : <span>切换 Agent</span>}
              </li>
            );
          }
          case "message_end":
          case "done":
          case "error":
            // 终态事件:由消息流与 runError 呈现,这里忽略
            return null;
          default:
            // 未知事件类型:跳过,保证前端对后端新事件健壮
            return null;
        }
      })}
    </ol>
  );
}

export function CollaborationPanel({
  events,
  taskPlan,
  taskResults,
  currentAgent,
}: CollaborationPanelProps) {
  // 默认展开;点击标题栏按钮折叠(客户端交互,SSR 只渲染初始展开态)
  const [expanded, setExpanded] = useState(true);
  // 活跃 Agent:优先取 store 的 currentAgent,缺省时从事件推导
  const activeAgent = currentAgent ?? lastSwitchAgent(events);
  // 空态:既无计划也无事件
  const isEmpty = events.length === 0 && taskPlan === null;

  return (
    <section
      className="overflow-hidden rounded-lg border border-border bg-card"
      data-slot="collaboration-panel"
    >
      <header
        className={cn(
          "flex items-center justify-between px-4 py-2",
          expanded && "border-b border-border",
        )}
      >
        <h3 className="text-caption font-medium text-foreground">协作过程</h3>
        <button
          aria-expanded={expanded}
          className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-caption text-muted-foreground hover:bg-muted hover:text-foreground"
          data-slot="collaboration-toggle"
          type="button"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "折叠" : "展开"}
          {expanded ? (
            <ChevronUp aria-hidden className="size-3.5" />
          ) : (
            <ChevronDown aria-hidden className="size-3.5" />
          )}
        </button>
      </header>

      {expanded ? (
        <>
          {/* 计划步骤条:task_plan 非 null 时展示,顶部叠加执行结果 */}
          {taskPlan ? <PlanSteps taskPlan={taskPlan} taskResults={taskResults} /> : null}
          {/* 事件时间线:events 非空时展示 */}
          {events.length > 0 ? <EventTimeline events={events} activeAgent={activeAgent} /> : null}
          {/* 空态:占位文案,不报错 */}
          {isEmpty ? (
            <p
              className="px-4 py-3 text-caption text-muted-foreground"
              data-slot="collaboration-empty"
            >
              暂无协作过程,提问后可见 Agent 协作事件。
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
