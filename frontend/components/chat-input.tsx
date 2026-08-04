"use client";

import { SendHorizontal, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useChatStore } from "@/stores/chat-store";

type SendShortcut = {
  isComposing: boolean;
  key: string;
  shiftKey: boolean;
};

type ChatInputContentProps = {
  isSending: boolean;
  // D4-T3:流式进行中——为 true 时停止按钮替代发送按钮。
  // 可选,向后兼容既有调用方(默认 false)。
  isStreaming?: boolean;
  onChange(value: string): void;
  // D4-T3:停止生成回调(可选,向后兼容)。无回调时停止按钮禁用。
  onStop?: () => void;
  onSubmit(): void;
  value: string;
};

// D4-T3:输入区自适应高度的夹取边界。最小基线 96px(约 3 行文本 +
// 上下 padding,与旧 rows={3} + min-h-24 视觉一致);上限 192px
// (8 行 × 24px,text-body 行高),超限由组件滚动。
export const MIN_TEXTAREA_HEIGHT = 96;
export const MAX_TEXTAREA_HEIGHT = 192;

// D4-T3:自适应高度夹取纯函数——scrollHeight(内容高度)夹在最小
// 基线与 maxHeight 之间。抽为纯函数便于 SSR 环境直接单测(组件
// 交互无法在 renderToStaticMarkup 下触发)。
export function clampTextareaHeight(scrollHeight: number, maxHeight: number): number {
  return Math.min(Math.max(scrollHeight, MIN_TEXTAREA_HEIGHT), maxHeight);
}

// D4-T3:按内容调整 textarea 高度——先复位为 auto 让 scrollHeight
// 反映真实内容,再夹取到上限。SSR 下无 DOM(ref 为 null)直接跳过,
// 初始高度由 min-h-24 兜底。onChange 与 useEffect([value]) 两处调用
// (输入即时 + 外部值变化,如提交清空后回落基线)。
function resizeTextarea(textarea: HTMLTextAreaElement | null): void {
  if (!textarea) {
    return;
  }
  textarea.style.height = "auto";
  textarea.style.height = `${clampTextareaHeight(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
}

export function normalizeMessage(value: string): string | null {
  const message = value.trim();
  return message || null;
}

export function isSendShortcut({ isComposing, key, shiftKey }: SendShortcut) {
  return key === "Enter" && !shiftKey && !isComposing;
}

export function ChatInputContent({
  isSending,
  isStreaming = false,
  onChange,
  onStop,
  onSubmit,
  value,
}: ChatInputContentProps) {
  const isEmpty = normalizeMessage(value) === null;
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // D4-T3:自适应高度——外部值变化(如提交后清空)时同步高度;
  // 输入中的即时调整走 onChange(见 textarea),避免 effect 滞后一帧。
  useEffect(() => {
    resizeTextarea(textareaRef.current);
  }, [value]);

  return (
    <form
      className="flex items-end gap-3"
      data-slot="chat-input"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      {/* D4-T3:rows 仅作初始行数(SSR 无 JS 时的初始态,既有测试依赖);
          挂载后高度由 resizeTextarea 接管。 */}
      <textarea
        aria-label="输入消息"
        className="min-h-24 max-h-48 flex-1 resize-none overflow-y-auto rounded-lg border border-input bg-background px-3 py-2 text-body text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted"
        disabled={isSending}
        onChange={(event) => {
          onChange(event.target.value);
          // D4-T3:输入时同步调整高度(无需等 effect)
          resizeTextarea(event.currentTarget);
        }}
        onKeyDown={(event) => {
          if (
            isSendShortcut({
              isComposing: event.nativeEvent.isComposing,
              key: event.key,
              shiftKey: event.shiftKey,
            })
          ) {
            event.preventDefault();
            onSubmit();
          }
        }}
        placeholder="输入消息，Enter 发送，Shift+Enter 换行"
        rows={3}
        ref={textareaRef}
        value={value}
      />
      {isStreaming ? (
        // D4-T3:流式进行中——停止按钮替代发送按钮(发送中本就锁定
        // 输入,替代比并排更清晰);无 onStop(旧调用方)时禁用兜底。
        <Button
          className="text-destructive"
          data-slot="stop-generating"
          disabled={!onStop}
          onClick={onStop}
          type="button"
          variant="outline"
        >
          <Square aria-hidden className="size-4" />
          停止生成
        </Button>
      ) : (
        <Button disabled={isSending || isEmpty} type="submit">
          <SendHorizontal aria-hidden className="size-4" />
          发送
        </Button>
      )}
    </form>
  );
}

export function ChatInput() {
  const cancelStreaming = useChatStore((state) => state.cancelStreaming);
  const isSending = useChatStore((state) => state.isSending);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const streamSendMessage = useChatStore((state) => state.streamSendMessage);
  const [value, setValue] = useState("");

  const handleSubmit = () => {
    const message = normalizeMessage(value);
    // 流式期间同样锁定输入(与发送中一致):并发重复提交会互相覆盖
    // 流状态(review 修正——sendMessage 时期只有 isSending,切换流式后
    // 必须同时看 isStreaming)。
    if (!message || isSending || isStreaming) {
      return;
    }

    setValue("");
    void streamSendMessage(message);
  };

  return (
    <ChatInputContent
      isSending={isSending || isStreaming}
      isStreaming={isStreaming}
      onChange={setValue}
      onStop={cancelStreaming}
      onSubmit={handleSubmit}
      value={value}
    />
  );
}
