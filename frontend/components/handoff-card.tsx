"use client";

// D2-T3:待审批手递交接卡片(纯展示)。
// 展示后端 PendingHandoff 的审批信息(目标 Agent、任务内容、计划步骤),
// 提供确认/拒绝两个操作。所有数据与回调由父组件(ConversationPanel)从
// store 订阅后以 props 传入,自身不订阅 store,便于 SSR 渲染与组件测试。
// 对 null(pending 为空)直接不渲染;对决策中(isDeciding)与错误文案健壮。
// D2-T4:新增「修改并继续」入口——展开编辑区修改目标 Agent(下拉)与任务
// 内容(文本域)后提交 modify 决策。编辑区展开/下拉值/文本域值均为组件
// 内部 useState(纯展示组件不写 store);SSR 初始不展开,编辑区交互逻辑由
// store 层透传测试与代码审查保障。本地校验:提交的修改至少一项与原始值
// 不同(任务内容需非空白),与后端「modify 至少携带一个非空修改字段」的
// 422 语义对齐。
import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";

import { AgentBadge } from "@/components/agent-badge";
import { Button } from "@/components/ui/button";
import type { components } from "@/contracts/api.generated";

type PendingHandoff = components["schemas"]["PendingHandoff"];
type WorkerAgentRole = components["schemas"]["WorkerAgentRole"];

export type HandoffDecisionAction = "confirm" | "reject" | "modify";

// D2-T4:modify 的修改字段(组件层 camelCase,store 转契约 snake_case 发送)
export type HandoffModifications = {
  targetAgent?: WorkerAgentRole;
  taskContent?: string;
};

export type HandoffCardProps = {
  // 决策相关错误文案(store requestError 映射后传入,仅审批相关错误码)
  errorMessage?: string | null;
  isDeciding: boolean;
  // D2-T4:action 扩展 modify;modifications 仅在 modify 时由组件组装传入
  onDecide: (
    action: HandoffDecisionAction,
    modifications?: HandoffModifications,
  ) => void;
  pending: PendingHandoff | null;
};

// D2-T4:可修改的目标 Agent 选项(WorkerAgentRole 值子集,与后端契约一致;
// 不含 evaluator——审批场景的目标只可能是授课/学习助理)
const MODIFIABLE_AGENTS: WorkerAgentRole[] = [
  "learning_assistant",
  "teaching_assistant",
];

export function HandoffCard({
  errorMessage,
  isDeciding,
  onDecide,
  pending,
}: HandoffCardProps) {
  // D2-T4:编辑区状态(展开/目标下拉值/文本域值/本地校验错误)
  const [isEditing, setIsEditing] = useState(false);
  const [targetAgent, setTargetAgent] = useState<WorkerAgentRole | null>(null);
  const [taskContent, setTaskContent] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  // D5-T5:卡片出现时焦点移入——容器 tabIndex={-1} 使其可聚焦。effect 内
  // 只做 DOM 焦点同步(focus()),不 setState(react-hooks lint:与外部系统
  // 同步合法)。依赖 [pending]:仅「出现/更换」时聚焦一次,isDeciding 等
  // 重渲染不抢焦点;SSR 不运行 effect,无 hydration 影响。
  // 取舍:卡片不是模态对话框——确认/拒绝/修改是主动操作,不做 Esc 关闭;
  // 决定后卡片卸载,焦点落回 body(浏览器默认),不额外归还到触发按钮
  // (触发按钮在父组件 ConversationPanel,跨组件归还复杂度超出验收口径
  // 「打开时进入、关闭时归还」)。
  const cardRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!pending) {
      return;
    }
    cardRef.current?.focus();
  }, [pending]);

  if (!pending) {
    return null;
  }

  const { request } = pending;

  // D2-T4:打开编辑区——以原始提案值为下拉/文本域初始值
  const startEditing = () => {
    setTargetAgent(request.target_agent);
    setTaskContent(request.task_content);
    setLocalError(null);
    setIsEditing(true);
  };

  const cancelEditing = () => {
    setIsEditing(false);
    setLocalError(null);
  };

  // D2-T4:提交修改——本地校验「至少一项与原始值不同」(任务内容需非空白),
  // 与后端「modify 至少携带一个非空修改字段」的 422 语义对齐。
  const submitModification = () => {
    const changes: HandoffModifications = {};
    if (targetAgent !== null && targetAgent !== request.target_agent) {
      changes.targetAgent = targetAgent;
    }
    const trimmedTaskContent = taskContent.trim();
    if (trimmedTaskContent && trimmedTaskContent !== request.task_content) {
      changes.taskContent = trimmedTaskContent;
    }
    if (Object.keys(changes).length === 0) {
      setLocalError("请至少修改目标 Agent 或任务内容。");
      return;
    }
    onDecide("modify", changes);
  };

  return (
    <section
      // D5-T5:aria-live="polite"——卡片从无到有挂载时,读屏播报卡片内容
      // (标题「等待审批」+ 任务摘要);决策后卡片卸载即停止播报,状态变化
      // 可感知。取舍:aria-live 区域内含可聚焦按钮(确认/拒绝),理想做法
      // 是 live region 与交互区分离,此处保持简单(卡片整体为区域),
      // 播报内容短、不频繁,可接受。
      aria-live="polite"
      // D5-T2:审批卡片出现动画(tw-animate-css:淡入 + 底部轻滑入,时长/缓动
      // 对齐 D5-T1 tokens);pending 为 null 时组件不渲染(上方早退),动画只
      // 在卡片出现时播放一次;reduced-motion 由 globals.css 全局媒体查询关闭。
      className="overflow-hidden rounded-lg border border-border bg-card animate-in fade-in-0 slide-in-from-bottom-1 duration-[var(--app-duration-normal)] ease-[var(--app-ease-out)]"
      data-slot="handoff-card"
      // D5-T5:tabIndex={-1} + ref——可被程序化聚焦(焦点移入见上方 effect)
      ref={cardRef}
      tabIndex={-1}
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

      {/* D2-T4:修改编辑区——初始收起,点击「修改并继续」后展开 */}
      {isEditing ? (
        <div
          className="space-y-3 border-t border-border px-4 py-3"
          data-slot="handoff-modify-editor"
        >
          <div className="flex items-center gap-2">
            <label
              className="text-caption text-muted-foreground"
              htmlFor="handoff-modify-target"
            >
              目标 Agent
            </label>
            <select
              className="rounded-md border border-border bg-background px-2 py-1 text-body text-foreground"
              data-slot="handoff-modify-target"
              disabled={isDeciding}
              id="handoff-modify-target"
              onChange={(event) =>
                setTargetAgent(event.target.value as WorkerAgentRole)
              }
              value={targetAgent ?? ""}
            >
              {MODIFIABLE_AGENTS.map((agent) => (
                <option key={agent} value={agent}>
                  {agent}
                </option>
              ))}
            </select>
            {/* 复用 AgentBadge 展示当前选中的目标 Agent */}
            {targetAgent ? <AgentBadge agent={targetAgent} /> : null}
          </div>

          <div className="space-y-1">
            <label
              className="text-caption text-muted-foreground"
              htmlFor="handoff-modify-task"
            >
              任务内容
            </label>
            <textarea
              className="w-full rounded-md border border-border bg-background px-2 py-1 text-body text-foreground"
              data-slot="handoff-modify-task"
              disabled={isDeciding}
              id="handoff-modify-task"
              onChange={(event) => setTaskContent(event.target.value)}
              rows={4}
              value={taskContent}
            />
          </div>

          {/* 本地校验错误(至少修改一项) */}
          {localError ? (
            <p
              className="text-caption text-destructive"
              data-slot="handoff-modify-error"
              role="alert"
            >
              {localError}
            </p>
          ) : null}

          <div className="flex items-center gap-2">
            <Button
              data-slot="handoff-modify-cancel"
              disabled={isDeciding}
              type="button"
              variant="outline"
              onClick={cancelEditing}
            >
              取消
            </Button>
            <Button
              data-slot="handoff-modify-submit"
              disabled={isDeciding}
              type="button"
              onClick={submitModification}
            >
              提交修改
            </Button>
          </div>
        </div>
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
        {/* D2-T4:修改入口——点击展开编辑区 */}
        <Button
          data-slot="handoff-modify"
          disabled={isDeciding}
          type="button"
          variant="outline"
          onClick={startEditing}
        >
          修改并继续
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
