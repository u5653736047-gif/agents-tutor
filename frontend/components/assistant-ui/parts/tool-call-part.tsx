"use client";

// assistant-ui 接入(T6):tool-call part 渲染器。
// 工具卡片:中文活动名(lib/tool-activity-labels 受控复制自旧面板)+ 状态
// 徽章 + 参数/结果折叠;配色复用 planStatusPresentation 的
// border-*/30 bg-*/10 text-* 语义 token 公式;data-slot 锚点(tool-row/
// tool-details)与旧面板/e2e 断言对齐。

import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { LoaderCircle } from "lucide-react";

import { toolActivityLabel } from "@/lib/tool-activity-labels";

export function ToolCallPart({
  toolName,
  argsText,
  result,
  isError,
}: ToolCallMessagePartProps) {
  const pending = result === undefined;
  return (
    <div
      className="rounded-md border border-border bg-muted/40 px-3 py-2"
      data-slot="tool-row"
    >
      <div className="flex items-center gap-2 text-caption font-medium">
        {pending ? (
          <LoaderCircle
            aria-hidden
            className="size-3.5 animate-spin text-muted-foreground"
          />
        ) : null}
        <span className="text-foreground" data-slot="tool-activity-label">
          {toolActivityLabel(toolName)}
        </span>
        <span
          className={
            pending
              ? "rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-primary"
              : isError
                ? "rounded-full border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-destructive"
                : "rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-success"
          }
        >
          {pending ? "执行中" : isError ? "失败" : "完成"}
        </span>
      </div>
      {argsText ? (
        <details className="mt-2" data-slot="tool-details">
          <summary className="cursor-pointer text-caption text-muted-foreground">
            参数
          </summary>
          <pre className="mt-1 overflow-x-auto rounded-sm bg-muted px-2 py-1.5 text-caption text-muted-foreground">
            {argsText}
          </pre>
        </details>
      ) : null}
      {!pending ? (
        <details className="mt-2" data-slot="tool-result">
          <summary className="cursor-pointer text-caption text-muted-foreground">
            结果
          </summary>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap rounded-sm bg-muted px-2 py-1.5 text-caption text-muted-foreground">
            {typeof result === "string" ? result : JSON.stringify(result)}
          </pre>
        </details>
      ) : null}
    </div>
  );
}
