"use client";

// assistant-ui 接入(T10):运行错误与网络错误块——从旧面板逐字移植
// (conversation-panel.tsx L402-477),锚点/文案/重试语义完全一致:
//   - run-error:runError 经 errorMessageFor 预设映射(分类标题/详情/动作),
//     onRetry 存在时给重试按钮;retryLastMessage 内部通道选择与首次发送一致
//     (流式优先),「断线不重发」语义由 store 保证(本组件只呈现);
//   - request-error-network:仅 code===null(网络失败/超时)在消息流内显示,
//     其余错误码由侧栏等现有路径处理,不重复提示。
// store 连接说明:与 ApprovalCards 同一模式(薄订阅 + 纯呈现)。

import { CircleAlert, WifiOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { errorMessageFor } from "@/lib/error-messages";
import { useChatStore } from "@/stores/chat-store";

export function RunErrorBlocks() {
  const lastSentMessage = useChatStore((state) => state.lastSentMessage);
  const requestError = useChatStore((state) => state.requestError);
  const retryLastMessage = useChatStore((state) => state.retryLastMessage);
  const runError = useChatStore((state) => state.runError);

  // 与旧面板同一守卫:有上一条消息才提供重试入口
  const onRetry = lastSentMessage ? () => void retryLastMessage() : undefined;
  const runErrorPreset = runError ? errorMessageFor(runError.error_code) : null;
  const networkPreset = errorMessageFor(null);

  return (
    <>
      {runError && runErrorPreset ? (
        <div
          className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3"
          data-slot="run-error"
          role="alert"
        >
          <CircleAlert
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-destructive"
          />
          <div className="min-w-0 flex-1">
            <p className="text-caption font-medium text-destructive">
              {runErrorPreset.title}
            </p>
            <p className="mt-0.5 text-caption text-muted-foreground">
              {runErrorPreset.detail}
            </p>
            {runError.message && runError.message !== runErrorPreset.detail ? (
              <p className="mt-0.5 text-caption text-muted-foreground/80">
                {runError.message}
              </p>
            ) : null}
            {onRetry ? (
              <Button
                className="mt-2"
                data-slot="run-error-retry"
                onClick={onRetry}
                size="sm"
                type="button"
                variant="outline"
              >
                {runErrorPreset.action ?? "重试"}
              </Button>
            ) : runErrorPreset.action ? (
              <p className="mt-0.5 text-caption text-muted-foreground/80">
                {runErrorPreset.action}
              </p>
            ) : null}
          </div>
        </div>
      ) : null}

      {requestError?.code === null ? (
        <div
          className="flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3"
          data-slot="request-error-network"
          role="alert"
        >
          <WifiOff
            aria-hidden
            className="mt-0.5 size-4 shrink-0 text-destructive"
          />
          <div className="min-w-0 flex-1">
            <p className="text-caption font-medium text-destructive">
              {networkPreset.title}
            </p>
            <p className="mt-0.5 text-caption text-muted-foreground">
              {networkPreset.detail}
            </p>
            {onRetry ? (
              <Button
                className="mt-2"
                data-slot="request-error-retry"
                onClick={onRetry}
                size="sm"
                type="button"
                variant="outline"
              >
                {networkPreset.action ?? "重试"}
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
