"use client";

// assistant-ui 接入(T10):在飞消息的空态骨架 part。
// 复刻旧流式气泡的「首事件前」骨架(conversation-panel.tsx L377-386):
// isStreaming 已起、首个增量未到时消息无任何 parts——MessagePrimitive.Parts
// 的 Empty 槽位渲染本组件(两行灰条,animate-pulse 呼吸);骨架是视觉占位,
// aria-hidden 避免读屏朗读噪音(进行中状态由 sr-only live-status 播报)。

import type { MessagePartStatus } from "@assistant-ui/react";

import { Skeleton } from "@/components/ui/skeleton";

export function StreamingEmpty({ status }: { status: MessagePartStatus }) {
  // 仅在运行中展示骨架;非运行态的空消息零渲染(不占位)
  if (status.type !== "running") {
    return null;
  }
  return (
    <div aria-hidden className="mt-2 space-y-2" data-slot="streaming-skeleton">
      <Skeleton className="h-3 w-full" />
      <Skeleton className="h-3 w-4/5" />
    </div>
  );
}
