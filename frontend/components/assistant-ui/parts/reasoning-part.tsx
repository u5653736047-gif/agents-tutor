"use client";

// assistant-ui 接入(T4 基础版,T5 增强):reasoning part 渲染器。
// 复刻 collaboration-panel.tsx L226-253 的 <details open={流式中}> 语义:
// 流式期间自动展开实时可见,结束后默认折叠;data-slot 锚点与 e2e 既有
// 断言(reasoning-block)对齐。

import type { ReasoningMessagePartProps } from "@assistant-ui/react";
import { LoaderCircle } from "lucide-react";

export function ReasoningPart({ text, status }: ReasoningMessagePartProps) {
  const running = status.type === "running";
  return (
    <details
      className="rounded-md border border-border bg-muted/40 px-3 py-2"
      data-slot="reasoning-block"
      open={running}
    >
      <summary className="flex cursor-pointer items-center gap-2 text-caption font-medium text-muted-foreground">
        {running ? (
          <LoaderCircle aria-hidden className="size-3.5 animate-spin" />
        ) : null}
        思维链
      </summary>
      <div className="mt-2 whitespace-pre-wrap text-caption text-muted-foreground">
        {text}
      </div>
    </details>
  );
}
