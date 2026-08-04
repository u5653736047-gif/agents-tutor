"use client";

import { SendHorizontal, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { applyCommand, filterCommands, isSlashCandidate, type SlashCommand } from "@/lib/slash-commands";
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

  // D4-T4:快捷指令候选列表状态。showCommands 由输入变化打开、选中/
  // 关闭时复位;selectedIndex 供方向键循环移动(onChange 时一并复位
  // 到 0)。SSR 初始态两者均为初始值,候选列表恒不渲染。
  const [showCommands, setShowCommands] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  const candidates = filterCommands(value);
  const showList = showCommands && isSlashCandidate(value) && candidates.length > 0;

  // D4-T4:选中候选——把 "/前缀" 替换为完整指令名(保留后续内容,
  // 如 "/p 支持向量机" → "/path 支持向量机")并关闭列表。指令前缀
  // 随消息一并发出,由 Supervisor 侧 Prompt 消费(本期不做后端解析)。
  const selectCommand = (command: SlashCommand) => {
    // review nit:选中后补一个尾随空格——用户紧接着输入正文时不会
    // 被吞成 "/explain支持向量机"(已含空格如 "/path 学习路径" 则
    // 不重复补;发送时 normalizeMessage 会 trim 掉多余空格)。
    const next = applyCommand(value, command);
    onChange(next.includes(" ") ? next : `${next} `);
    setShowCommands(false);
  };

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
      {/* D4-T4:候选列表与 textarea 同处相对定位容器,列表悬浮在
          textarea 上方(bottom-full),不挤占输入区高度。 */}
      <div className="relative flex-1">
        {showList && (
          <ul
            aria-label="快捷指令候选"
            className="absolute bottom-full left-0 right-0 z-10 mb-2 overflow-hidden rounded-md border border-border bg-card shadow-lg"
            data-slot="slash-commands"
            role="listbox"
          >
            {candidates.map((command, index) => (
              <li
                aria-selected={index === selectedIndex}
                className={`flex cursor-pointer items-baseline gap-2 px-3 py-2 text-body ${
                  index === selectedIndex
                    ? "bg-primary/10 text-primary"
                    : "text-foreground hover:bg-muted"
                }`}
                data-slot="slash-command-item"
                key={command.name}
                // 容器 role="listbox" 时,项必须是 role="option" 才支持
                // aria-selected(lint jsx-a11y 校验)。
                role="option"
                // 用 onMouseDown 而非 onClick:先于 textarea 失焦触发,
                // 选中后列表即关闭,点击不会落空。
                onMouseDown={(event) => {
                  event.preventDefault();
                  selectCommand(command);
                }}
              >
                <span className="shrink-0 font-medium">/{command.name}</span>
                <span className="truncate text-muted-foreground">{command.description}</span>
                <span className="ml-auto shrink-0 text-caption text-muted-foreground">
                  {command.example}
                </span>
              </li>
            ))}
          </ul>
        )}
        {/* D4-T3:rows 仅作初始行数(SSR 无 JS 时的初始态,既有测试依赖);
            挂载后高度由 resizeTextarea 接管。 */}
        <textarea
          aria-label="输入消息"
          className="min-h-24 max-h-48 w-full resize-none overflow-y-auto rounded-lg border border-input bg-background px-3 py-2 text-body text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:bg-muted"
          disabled={isSending}
          onChange={(event) => {
            onChange(event.target.value);
            // D4-T4:输入变化即打开候选列表并复位选中项(是否真正显示
            // 由 showList 派生条件把关,如输入空格后自动关闭)。
            setShowCommands(true);
            setSelectedIndex(0);
            // D4-T3:输入时同步调整高度(无需等 effect)
            resizeTextarea(event.currentTarget);
          }}
          onKeyDown={(event) => {
            // D4-T4:候选列表打开时优先拦截导航/确认键,未打开时
            // 回落既有发送快捷键逻辑,两者互不干扰。
            if (showList) {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setSelectedIndex((index) => (index + 1) % candidates.length);
                return;
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setSelectedIndex((index) => (index - 1 + candidates.length) % candidates.length);
                return;
              }
              if (event.key === "Enter") {
                // 选中当前候选(selectedIndex 由循环取模保证在界内,
                // 守卫仅为满足 noUncheckedIndexedAccess)。
                event.preventDefault();
                const command = candidates[selectedIndex];
                if (command) {
                  selectCommand(command);
                }
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                setShowCommands(false);
                return;
              }
            }
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
      </div>
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
