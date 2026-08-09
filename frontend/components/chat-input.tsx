"use client";

import { Paperclip, SendHorizontal, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type ChangeEvent } from "react";

import { Button } from "@/components/ui/button";
import { uploadFile, type AttachmentInput } from "@/lib/api-client";
import { applyCommand, filterCommands, isSlashCandidate, type SlashCommand } from "@/lib/slash-commands";
import { useChatStore } from "@/stores/chat-store";

type SendShortcut = {
  isComposing: boolean;
  key: string;
  shiftKey: boolean;
};

// D7-T2:附件上限——与后端白名单(.pdf/.png/.jpg/.jpeg/.txt)无关,
// 是前端一次可携带的附件数量上限;超出部分截断并提示。
export const MAX_ATTACHMENTS = 3;

// D7-T2:待上传附件(本地数组,受控展示)。status 状态机:
// pending(已选未传)→ uploading(上传中)→ error(失败,可重试)。
// 上传成功不回写状态——回执随消息提交后整项清空;error 项保留
// 直到用户「重试」(重置回 pending)或移除。
type PendingFile = {
  file: File;
  status: "pending" | "uploading" | "error";
  errorMessage?: string;
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
  // D7-T2:带附件提交(附件已完成上传、回执已组装)。可选,向后
  // 兼容:未提供时 ChatInputContent 回落无附件 onSubmit(附件回执
  // 被丢弃,仅旧调用方场景)。ChatInput 容器组装 ChatRequest 时使用。
  onSubmitWithAttachments?(message: string, attachments: AttachmentInput[]): void;
  value: string;
};

// 输入区自适应高度的夹取边界。最小基线 64px（约 2 行文本），保留
// 足够输入空间但不再让空编辑器长期占据大块视口；上限 192px
// (8 行 × 24px,text-body 行高),超限由组件滚动。
export const MIN_TEXTAREA_HEIGHT = 64;
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
  onSubmitWithAttachments,
  value,
}: ChatInputContentProps) {
  const isEmpty = normalizeMessage(value) === null;
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // D7-T2:隐藏 file input 的 ref——附件按钮点击时触发选择;选择后
  // 清空 value 以允许重复选择同一文件(受控 value 不适用于 file input)。
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // D4-T4:快捷指令候选列表状态。showCommands 由输入变化打开、选中/
  // 关闭时复位;selectedIndex 供方向键循环移动(onChange 时一并复位
  // 到 0)。SSR 初始态两者均为初始值,候选列表恒不渲染。
  const [showCommands, setShowCommands] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  // D7-T2:待上传附件列表与「超出上限」提示。两者分开:limit hint
  // 是瞬时提示(截断时置位,下次未截断的选择/移除时清除);error
  // 提示由 pendingFiles 的 error 项派生,无需单独状态。
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [attachLimitExceeded, setAttachLimitExceeded] = useState(false);

  const candidates = filterCommands(value);
  const showList = showCommands && isSlashCandidate(value) && candidates.length > 0;

  // D7-T2:附件区禁用条件——发送中/流式中/上传中任一状态都锁定
  // 附件操作(上传中锁定是为防止上传期间改列表导致索引错乱)。
  const isUploading = pendingFiles.some((pending) => pending.status === "uploading");
  const attachmentsDisabled = isSending || isStreaming || isUploading;

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

  // D7-T2:选择文件——追加到 pendingFiles(上限 MAX_ATTACHMENTS,
  // 超出部分截断并提示)。同文件重复选择允许(value 已清空);上限
  // 已满时整批忽略。File 对象只在事件处理器内创建,渲染路径无
  // window/File 依赖,SSR 安全。
  const handleFilesSelected = (event: ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) {
      return;
    }
    const incoming = Array.from(files);
    const room = MAX_ATTACHMENTS - pendingFiles.length;
    if (room <= 0) {
      setAttachLimitExceeded(true);
    } else {
      const accepted = incoming.slice(0, room);
      setPendingFiles((prev) => [
        ...prev,
        ...accepted.map((file) => ({ file, status: "pending" as const })),
      ]);
      setAttachLimitExceeded(incoming.length > room);
    }
    // 清空 input value:允许再次选择同一文件(React 对 file input
    // 不接管 value,直接操作 DOM 属性即可)。
    event.target.value = "";
  };

  // D7-T2:移除附件——按索引过滤,同时清除上限提示。
  const removePendingFile = (index: number) => {
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachLimitExceeded(false);
  };

  // D7-T2:重试失败附件——重置回 pending(清除 errorMessage),下次
  // 发送时随队列重新上传。上传统一发生在发送时刻,重试不单独触发
  // 上传,避免「重传成功但消息未发」的悬置状态。
  const retryPendingFile = (index: number) => {
    setPendingFiles((prev) =>
      prev.map((pending, i) =>
        i === index ? { file: pending.file, status: "pending" as const } : pending,
      ),
    );
  };

  // D7-T2:带附件提交——逐文件上传(顺序执行,最多 3 个;error 项
  // 跳过等待用户重试/移除),收集成功回执组装 Attachment 列表:
  //   file_id/name/content_type/size 直接取自上传回执(契约同源字段)。
  // 全部失败:不发送,消息与 error 态保留;部分失败:成功项随消息
  // 提交,失败项保留(error chip + 提示行)。提交后清空已提交项,
  // error 项保留可重试。
  const submitWithAttachments = async (message: string) => {
    const attachments: AttachmentInput[] = [];
    for (let index = 0; index < pendingFiles.length; index += 1) {
      const pending = pendingFiles[index];
      if (!pending || pending.status === "error") {
        // 上次失败的附件不自动重传:跳过,等待「重试」或移除
        continue;
      }
      setPendingFiles((prev) =>
        prev.map((item, i) => (i === index ? { ...item, status: "uploading" as const } : item)),
      );
      try {
        const receipt = await uploadFile(pending.file);
        attachments.push({
          file_id: receipt.file_id,
          name: receipt.name,
          content_type: receipt.content_type,
          size: receipt.size,
        });
      } catch (error) {
        setPendingFiles((prev) =>
          prev.map((item, i) =>
            i === index
              ? {
                  ...item,
                  status: "error" as const,
                  errorMessage: error instanceof Error ? error.message : "上传失败，请重试。",
                }
              : item,
          ),
        );
      }
    }
    if (attachments.length === 0) {
      // 全部失败:不发送消息,error 态与输入文本保留
      return;
    }
    if (onSubmitWithAttachments) {
      onSubmitWithAttachments(message, attachments);
    } else {
      // 旧调用方未接附件通道:回落无附件提交(回执丢弃)
      onSubmit();
    }
    // 发送后清空已提交项(成功项随消息走),error 项保留可重试
    setPendingFiles((prev) => prev.filter((pending) => pending.status === "error"));
  };

  // D7-T2:统一提交入口——无附件走原 onSubmit(文本 + 发送快捷键
  // 语义不变);有附件先上传再提交(异步,期间发送按钮锁定)。
  const handleSubmit = () => {
    const message = normalizeMessage(value);
    if (!message || isSending || isStreaming || isUploading) {
      return;
    }
    if (pendingFiles.length === 0) {
      onSubmit();
      return;
    }
    void submitWithAttachments(message);
  };

  return (
    <form
      className="flex items-end gap-2 rounded-2xl border border-border/80 bg-card p-2 shadow-sm"
      data-slot="chat-input"
      onSubmit={(event) => {
        event.preventDefault();
        handleSubmit();
      }}
    >
      {/* D7-T2:附件按钮——触发隐藏 file input(accept 与后端白名单
          .pdf/.png/.jpg/.jpeg/.txt 对齐,见 backend api/files.py)。 */}
      <Button
        aria-label="添加附件"
        className="shrink-0"
        data-slot="attach-button"
        disabled={attachmentsDisabled}
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
        {/* D7-T2:附件区——chips 在 textarea 上方占文档流(不悬浮),
            与候选列表的 absolute 定位互不干扰。chip 样式走 DESIGN_SYSTEM
            徽章公式(rounded-full border px-2 py-0.5 text-caption),error
            态按「border-{色}/30 bg-{色}/10 text-{色}」变体。 */}
        {(pendingFiles.length > 0 || attachLimitExceeded) && (
          <div
            className="mb-2 flex flex-wrap items-center gap-2"
            data-slot="attachment-area"
          >
            {pendingFiles.map((pending, index) => (
              <span
                className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-caption font-medium ${
                  pending.status === "error"
                    ? "border-destructive/30 bg-destructive/10 text-destructive"
                    : "border-border bg-card text-foreground"
                }`}
                data-slot="attachment-chip"
                key={index}
              >
                <span className="max-w-40 truncate">{pending.file.name}</span>
                {pending.status === "uploading" && (
                  <span className="font-normal text-muted-foreground">上传中…</span>
                )}
                {pending.status === "error" && (
                  <>
                    <span className="max-w-40 truncate font-normal">
                      {pending.errorMessage ?? "上传失败"}
                    </span>
                    <button
                      className="rounded px-1.5 font-normal hover:bg-destructive/10"
                      data-slot="attachment-retry"
                      disabled={attachmentsDisabled}
                      onClick={() => retryPendingFile(index)}
                      type="button"
                    >
                      重试
                    </button>
                  </>
                )}
                <button
                  aria-label={`移除附件 ${pending.file.name}`}
                  className="rounded p-0.5 hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                  data-slot="attachment-remove"
                  disabled={attachmentsDisabled}
                  onClick={() => removePendingFile(index)}
                  type="button"
                >
                  <X aria-hidden className="size-3" />
                </button>
              </span>
            ))}
            {attachLimitExceeded && (
              <p
                className="w-full text-caption text-muted-foreground"
                data-slot="attach-limit-hint"
              >
                最多附加 {MAX_ATTACHMENTS} 个文件，超出部分已忽略。
              </p>
            )}
            {pendingFiles.some((pending) => pending.status === "error") && (
              <p
                className="w-full text-caption text-destructive"
                data-slot="attach-error-hint"
              >
                附件上传失败，已跳过。可点「重试」重新上传或移除。
              </p>
            )}
          </div>
        )}
        {/* D4-T3:rows 仅作初始行数(SSR 无 JS 时的初始态,既有测试依赖);
            挂载后高度由 resizeTextarea 接管。 */}
        <textarea
          aria-label="输入消息"
          className="min-h-16 max-h-48 w-full resize-none overflow-y-auto rounded-xl border-0 bg-transparent px-2 py-2 text-body text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-0 disabled:cursor-not-allowed disabled:bg-muted/40"
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
              handleSubmit();
            }
          }}
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          rows={2}
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
        <Button disabled={isSending || isStreaming || isUploading || isEmpty} type="submit">
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

  // D7-T2:带附件提交——附件已由 ChatInputContent 上传并组装回执,
  // 这里直接透传给 store。附件走流式主通道(stream-client 已扩展
  // attachments 透传,与同步通道同契约;重试耗尽才降级同步,见
  // streamSendMessage 内注释),文本清空与无附件路径一致。
  const handleSubmitWithAttachments = (message: string, attachments: AttachmentInput[]) => {
    if (isSending || isStreaming) {
      return;
    }
    setValue("");
    void streamSendMessage(message, attachments);
  };

  return (
    <ChatInputContent
      isSending={isSending || isStreaming}
      isStreaming={isStreaming}
      onChange={setValue}
      onStop={cancelStreaming}
      onSubmit={handleSubmit}
      onSubmitWithAttachments={handleSubmitWithAttachments}
      value={value}
    />
  );
}
