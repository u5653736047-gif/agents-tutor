"use client";

// assistant-ui 接入(T14):原生 Composer 输入区(独立子开关
// assistant-ui-composer,默认关——旧 ChatInput 仍是生产路径)。
//
// 与 ChatInput 的行为对齐清单:
//   - Enter 发送 / Shift+Enter 换行 / IME 合成中不发送(库内置,见
//     ComposerInput 的 isComposing 守卫);
//   - slash 命令候选(复用 lib/slash-commands 与同一交互状态机);
//   - 附件:数量上限 MAX_ATTACHMENTS=3、发送时上传(attachment-adapter
//     的 composer-send 语义)、chip 可移除;
//   - 流式中停止按钮替换发送按钮(cancelStreaming);
//   - 发送闸门:isSending/isStreaming/isDecidingToolApproval/
//     pendingToolApproval(与 ChatInput 的 isBlocked 逐项一致)。
//
// 数据流:ComposerPrimitive → runtime.append → runtime-provider.onNew
// (文本 + 附件回执还原)→ streamSendMessage——与 ChatInput 直连殊途同归。

import {
  AttachmentPrimitive,
  ComposerPrimitive,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import { LoaderCircle, Paperclip, SendHorizontal, Square, X } from "lucide-react";
import { useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { MAX_ATTACHMENTS } from "@/components/chat-input";
import {
  applyCommand,
  filterCommands,
  isSlashCandidate,
  type SlashCommand,
} from "@/lib/slash-commands";
import { useChatStore } from "@/stores/chat-store";

// 单个附件 chip:名称 + 移除(上传在发送时发生,这里无状态展示)
function AttachmentChip() {
  return (
    <AttachmentPrimitive.Root
      className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-card px-2 py-0.5 text-caption font-medium text-foreground"
      data-slot="attachment-chip"
    >
      <span className="max-w-40 truncate">
        <AttachmentPrimitive.Name />
      </span>
      <AttachmentPrimitive.Remove asChild>
        <button
          aria-label="移除附件"
          className="rounded p-0.5 hover:bg-muted"
          data-slot="attachment-remove"
          type="button"
        >
          <X aria-hidden className="size-3" />
        </button>
      </AttachmentPrimitive.Remove>
    </AttachmentPrimitive.Root>
  );
}

export function ComposerNative() {
  const aui = useAui();
  const composerText = useAuiState((state) => state.composer.text);
  const attachmentCount = useAuiState(
    (state) => state.composer.attachments.length,
  );
  const cancelStreaming = useChatStore((state) => state.cancelStreaming);
  const isSending = useChatStore((state) => state.isSending);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const isDecidingToolApproval = useChatStore(
    (state) => state.isDecidingToolApproval,
  );
  const pendingToolApproval = useChatStore((state) => state.pendingToolApproval);

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // slash 候选交互状态机(与 ChatInput 同一语义:输入打开、选择/关闭复位)
  const [showCommands, setShowCommands] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [attachLimitExceeded, setAttachLimitExceeded] = useState(false);

  const candidates = filterCommands(composerText);
  const showList =
    showCommands && isSlashCandidate(composerText) && candidates.length > 0;
  const isBlocked =
    isSending ||
    isStreaming ||
    isDecidingToolApproval ||
    pendingToolApproval !== null;

  const selectCommand = (command: SlashCommand) => {
    const next = applyCommand(composerText, command);
    aui.composer.setText(next.includes(" ") ? next : `${next} `);
    setShowCommands(false);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!showList) {
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelectedIndex((index) => (index + 1) % candidates.length);
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelectedIndex(
        (index) => (index - 1 + candidates.length) % candidates.length,
      );
      return;
    }
    if (event.key === "Enter") {
      // 选中候选;preventDefault 同时拦下库内的 Enter 提交
      // (composeEventHandlers 尊重 defaultPrevented)
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
    }
  };

  const handleFilesSelected = (event: ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) {
      return;
    }
    const incoming = Array.from(files);
    const room = MAX_ATTACHMENTS - attachmentCount;
    if (room <= 0) {
      setAttachLimitExceeded(true);
    } else {
      for (const file of incoming.slice(0, room)) {
        void aui.composer.addAttachment(file);
      }
      setAttachLimitExceeded(incoming.length > room);
    }
    // 允许再次选择同一文件
    event.target.value = "";
  };

  return (
    <ComposerPrimitive.Root
      className="flex items-end gap-2 rounded-2xl border border-border/80 bg-card p-2 shadow-sm"
      data-slot="chat-input"
    >
      <Button
        aria-label="添加附件"
        className="shrink-0"
        data-slot="attach-button"
        disabled={isBlocked}
        onClick={() => fileInputRef.current?.click()}
        type="button"
        variant="outline"
      >
        <Paperclip aria-hidden className="size-4" />
      </Button>
      <input
        accept=".pdf,.png,.jpg,.jpeg,.txt"
        aria-label="选择附件"
        className="hidden"
        data-slot="attach-input"
        multiple
        onChange={handleFilesSelected}
        ref={fileInputRef}
        type="file"
      />
      <div className="relative flex-1">
        {showList ? (
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
                onMouseDown={(event) => {
                  event.preventDefault();
                  selectCommand(command);
                }}
                role="option"
              >
                <span className="shrink-0 font-medium">/{command.name}</span>
                <span className="truncate text-muted-foreground">
                  {command.description}
                </span>
                <span className="ml-auto shrink-0 text-caption text-muted-foreground">
                  {command.example}
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        {attachmentCount > 0 || attachLimitExceeded ? (
          <div
            className="mb-2 flex flex-wrap items-center gap-2"
            data-slot="attachment-area"
          >
            <ComposerPrimitive.Attachments
              components={{ Attachment: AttachmentChip }}
            />
            {attachLimitExceeded ? (
              <p
                className="w-full text-caption text-muted-foreground"
                data-slot="attach-limit-hint"
              >
                最多附加 {MAX_ATTACHMENTS} 个文件，超出部分已忽略。
              </p>
            ) : null}
          </div>
        ) : null}
        <ComposerPrimitive.Input
          aria-label="输入消息"
          className="min-h-16 max-h-48 w-full resize-none overflow-y-auto rounded-xl border-0 bg-transparent px-2 py-2 text-body text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-0 disabled:cursor-not-allowed disabled:bg-muted/40"
          disabled={isSending}
          onChange={() => {
            // 输入变化即打开候选并复位选中项(显示与否由 showList 派生把关)
            setShowCommands(true);
            setSelectedIndex(0);
          }}
          onKeyDown={handleKeyDown}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={2}
          submitMode="enter"
        />
      </div>
      {isStreaming && !isDecidingToolApproval ? (
        <Button
          className="text-destructive"
          data-slot="stop-generating"
          onClick={cancelStreaming}
          type="button"
          variant="outline"
        >
          <Square aria-hidden className="size-4" />
          停止生成
        </Button>
      ) : (
        <ComposerPrimitive.Send asChild>
          <Button data-slot="composer-send" disabled={isBlocked} type="submit">
            {isSending ? (
              <LoaderCircle aria-hidden className="size-4 animate-spin" />
            ) : (
              <SendHorizontal aria-hidden className="size-4" />
            )}
            发送
          </Button>
        </ComposerPrimitive.Send>
      )}
    </ComposerPrimitive.Root>
  );
}
