"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { CircleAlert, LoaderCircle, WifiOff } from "lucide-react";
import { useEffect, useRef } from "react";

import { AgentBadge } from "@/components/agent-badge";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import { ChatInput } from "@/components/chat-input";
import { CitationList } from "@/components/citation-list";
import { CollaborationPanel } from "@/components/collaboration-panel";
import { HandoffCard } from "@/components/handoff-card";
import { Button } from "@/components/ui/button";
import type { AgentRole } from "@/lib/agent-roles";
import type { ApiClientError, ChatResponse, Message } from "@/lib/api-client";
import { errorMessageFor } from "@/lib/error-messages";
import { isNearBottom } from "@/lib/scroll-follow";
import { useChatStore } from "@/stores/chat-store";

function AssistantBadge({ agent }: { agent: AgentRole | null | undefined }) {
  if (agent) {
    return <AgentBadge agent={agent} />;
  }

  return (
    <span
      className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-caption font-medium text-muted-foreground"
      data-slot="assistant-badge-fallback"
    >
      助手
    </span>
  );
}

// D4-T8:单条消息行——ConversationContent(全量路径)与 ConversationPanel
// (虚拟化窗口路径)共用,渲染逻辑单点,保证两种模式输出一致。
// dataIndex/measureRef 仅在虚拟化路径传入:virtual-core 的 measureElement
// 靠 data-index 属性定位行索引(默认 indexAttribute),全量路径不输出
// 该属性,SSR 输出与既有完全一致。
type MessageRowProps = {
  message: Message;
  index: number;
  // 虚拟化路径:行在列表中的真实索引,渲染为 data-index 供 measureElement 定位
  dataIndex?: number;
  // 虚拟化路径:useVirtualizer 的 measureElement(ref 回调),动态校正行高
  measureRef?: (node: HTMLElement | null) => void;
};

export function MessageRow({
  message,
  index,
  dataIndex,
  measureRef,
}: MessageRowProps) {
  const isUser = message.role === "user";
  // 虚拟化行必须带 data-index(measureElement 依赖);全量路径不渲染
  const rowIndex = dataIndex ?? index;

  return (
    <article
      className={isUser ? "flex justify-end" : "flex justify-start"}
      data-index={measureRef ? rowIndex : undefined}
      data-message-role={message.role}
      ref={measureRef}
    >
      <div
        className={
          isUser
            ? "max-w-[80%] rounded-lg bg-primary px-4 py-3 text-body text-primary-foreground"
            : "max-w-[80%] rounded-lg border border-border bg-card px-4 py-3 text-body text-foreground"
        }
      >
        {!isUser ? <AssistantBadge agent={message.agent} /> : null}
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="mt-2">
            <AssistantMarkdown content={message.content} />
          </div>
        )}
      </div>
    </article>
  );
}

type ConversationContentProps = {
  isSending: boolean;
  isStreaming: boolean;
  messages: Message[];
  // D2-T5:可选重试回调(重发上一条消息);未提供时不渲染重试按钮
  onRetry?: () => void;
  // D2-T5:请求层错误——仅 code===null(网络失败/超时)在消息流内
  // 显示错误块 + 重试入口;其余码由侧栏统一映射展示
  requestError?: ApiClientError | null;
  runError: NonNullable<ChatResponse["run_error"]> | null;
  streamingAgent: AgentRole | null;
  streamingMessage: Message | null;
  // D4-T8:虚拟化窗口参数。null/缺省 = 未启用虚拟化,消息行全量渲染
  // (短会话既有行为);非 null = 长会话虚拟化,消息行由 ConversationPanel
  // 用 MessageRow 窗口渲染,这里只保留流式气泡/错误块/发送指示等尾部块。
  virtualItems?: { index: number }[] | null;
};

export function ConversationContent({
  isSending,
  isStreaming,
  messages,
  onRetry,
  requestError,
  runError,
  streamingAgent,
  streamingMessage,
  virtualItems,
}: ConversationContentProps) {
  // D2-T5:网络失败/超时(code===null)预设,供消息流下方的网络错误块使用
  const networkPreset = errorMessageFor(null);
  const runErrorPreset = runError ? errorMessageFor(runError.error_code) : null;

  return (
    <>
      {/* D4-T8:未启用虚拟化时全量渲染消息行(短会话既有行为);
          启用时消息行由 ConversationPanel 虚拟渲染,此处仅渲染尾部块 */}
      {virtualItems == null
        ? messages.map((message, index) => (
            <MessageRow
              key={message.created_at ?? `${message.role}-${index}`}
              message={message}
              index={index}
            />
          ))
        : null}

      {/* 流式气泡:isStreaming 期间渲染,或异常中断后保留已收到内容时继续展示 */}
      {isStreaming || streamingMessage ? (
        <article
          className="flex justify-start"
          data-message-role="assistant"
          data-slot="streaming-message"
        >
          <div className="max-w-[80%] rounded-lg border border-border bg-card px-4 py-3 text-body text-foreground">
            <AssistantBadge agent={streamingAgent} />
            <div className="mt-2">
              <AssistantMarkdown content={streamingMessage?.content ?? ""} />
            </div>
            {isStreaming ? (
              <div className="mt-2 flex items-center gap-2 text-caption text-muted-foreground">
                <LoaderCircle aria-hidden className="size-4 animate-spin" />
                正在生成…
              </div>
            ) : null}
          </div>
        </article>
      ) : null}

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

      {/* D2-T5:网络失败/超时(code===null)——消息流下方给出重试入口;
          其它 requestError 由侧栏映射展示,面板不重复提示 */}
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

      {isSending ? (
        <div className="flex items-center gap-2 text-caption text-muted-foreground">
          <LoaderCircle aria-hidden className="size-4 animate-spin" />
          正在生成回答…
        </div>
      ) : null}
    </>
  );
}

export function ConversationPanel() {
  // D2-T2:协作过程面板所需数据(计划、结果、事件、活跃 Agent)由这里订阅后传入
  const currentAgent = useChatStore((state) => state.currentAgent);
  // D2-T3:审批卡片数据(待审批项、决策中标记、决策错误、决策动作)
  const decideHandoff = useChatStore((state) => state.decideHandoff);
  const events = useChatStore((state) => state.events);
  const isDecidingHandoff = useChatStore((state) => state.isDecidingHandoff);
  const isSending = useChatStore((state) => state.isSending);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const lastSentMessage = useChatStore((state) => state.lastSentMessage);
  const messages = useChatStore((state) => state.messages);
  const pendingHandoff = useChatStore((state) => state.pendingHandoff);
  // D3-T4:本轮回答的引用列表(store 从 ChatResponse.references 归一),
  // null 时 CitationList 零渲染,无引用轮次不显示任何东西
  const references = useChatStore((state) => state.references);
  const requestError = useChatStore((state) => state.requestError);
  const retryLastMessage = useChatStore((state) => state.retryLastMessage);
  const runError = useChatStore((state) => state.runError);
  const streamingAgent = useChatStore((state) => state.streamingAgent);
  const streamingMessage = useChatStore((state) => state.streamingMessage);
  const taskPlan = useChatStore((state) => state.taskPlan);
  const taskResults = useChatStore((state) => state.taskResults);
  const endRef = useRef<HTMLDivElement>(null);
  // D4-T8:滚动容器 ref——既是 virtualizer 的 getScrollElement,
  // 也是 onScroll 贴底判定的目标元素
  const parentRef = useRef<HTMLDivElement>(null);
  // D4-T8:是否跟随底部(新消息自动滚动)。初始 true;用户上翻时
  // onScroll 置 false 暂停跟随,回到底部后自动恢复。
  const followBottom = useRef(true);

  // D4-T8:长会话(>50 条)启用消息列表虚拟化,只渲染视口附近的行,
  // 避免数千条消息的 DOM 开销;短会话禁用(虚拟化对小列表无收益,
  // 且动态测量有抖动),保持既有全量渲染行为与 SSR 输出。
  const virtualizer = useVirtualizer<HTMLDivElement, HTMLElement>({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    // 文本行高不定,96px 仅作首屏估算,measureElement 挂载后按实际校正
    estimateSize: () => 96,
    // 与既有 key 逻辑一致(created_at 优先,index 兜底)
    getItemKey: (index) => messages[index]?.created_at ?? `msg-${index}`,
    enabled: messages.length > 50,
    overscan: 8,
    // 与内容列 flex gap-4 一致:虚拟位置按「行高 + gap」累加,
    // 否则长列表底部会累积 16px×N 的偏差,滚动不到最后一条消息
    gap: 16,
  });
  const virtualItems = virtualizer.getVirtualItems();
  // 与 enabled 同源判断,渲染期直接可用(virtualizer 自身状态不参与)
  const isVirtualized = messages.length > 50;
  const totalSize = virtualizer.getTotalSize();

  const handleScroll = () => {
    const el = parentRef.current;
    if (!el) return;
    followBottom.current = isNearBottom(
      el.scrollTop,
      el.clientHeight,
      el.scrollHeight,
    );
  };

  useEffect(() => {
    // D4-T8:用户上翻浏览(followBottom=false)时不打扰,
    // 回到底部后由 onScroll 恢复跟随
    if (followBottom.current) {
      endRef.current?.scrollIntoView({ block: "end" });
    }
  }, [
    currentAgent,
    events,
    isDecidingHandoff,
    isSending,
    isStreaming,
    messages,
    pendingHandoff,
    references,
    runError,
    streamingAgent,
    streamingMessage,
    taskPlan,
    taskResults,
  ]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div
        className="min-h-0 flex-1 overflow-y-auto"
        data-slot="message-list"
        onScroll={handleScroll}
        ref={parentRef}
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-8 py-6">
          {/* D4-T8:虚拟化前 spacer——把首行推到估算位置(首帧未测量
              时为 0,随测量/滚动更新) */}
          {isVirtualized && virtualItems.length > 0 ? (
            <div style={{ height: virtualItems[0]?.start ?? 0 }} />
          ) : null}
          {isVirtualized
            ? virtualItems.map((item) => {
                // count 与 messages 同源,理论上不会缺项;防御 undefined
                // 避免越界渲染(noUncheckedIndexedAccess)
                const message = messages[item.index];
                if (!message) return null;
                return (
                  <MessageRow
                    dataIndex={item.index}
                    index={item.index}
                    key={message.created_at ?? `msg-${item.index}`}
                    measureRef={virtualizer.measureElement}
                    message={message}
                  />
                );
              })
            : null}
          <ConversationContent
            isSending={isSending}
            isStreaming={isStreaming}
            messages={messages}
            onRetry={
              lastSentMessage ? () => void retryLastMessage() : undefined
            }
            requestError={requestError}
            runError={runError ?? null}
            streamingAgent={streamingAgent}
            streamingMessage={streamingMessage}
            virtualItems={isVirtualized ? virtualItems : null}
          />
          {/* D2-T2:协作过程面板——消息流与输入区之间,展示计划与事件 */}
          <CollaborationPanel
            currentAgent={currentAgent}
            events={events}
            taskPlan={taskPlan}
            taskResults={taskResults}
          />
          {/* D2-T3:审批卡片——协作面板之后、输入区之前;错误文案只映射
              审批相关错误码,其它 requestError 仍由侧栏等现有路径处理。
              handoff_not_pending 不在此映射:store 收到该码会清除并刷新
              pending,卡片随之消失,错误行永远不会显示(死分支已删,
              review nit)。 */}
          <HandoffCard
            errorMessage={
              requestError?.code === "session_busy"
                ? requestError.message
                : null
            }
            isDeciding={isDecidingHandoff}
            onDecide={(action, modifications) =>
              void decideHandoff(action, modifications)
            }
            pending={pendingHandoff}
          />
          {/* D3-T4:引用卡片——消息列表尾部(审批卡片之后),引用对应
              最后一轮回答,跟随该轮的回答与协作过程一起展示;store
              的 references 为 null 时组件零渲染,不占位 */}
          <CitationList citations={references} />
          {/* D4-T8:虚拟化尾 spacer——补足未渲染行的估算高度,
              保证滚动条总高度与全量模式一致 */}
          {isVirtualized ? (
            <div
              style={{
                height: Math.max(
                  0,
                  totalSize - (virtualItems[virtualItems.length - 1]?.end ?? 0),
                ),
              }}
            />
          ) : null}
          <div data-slot="conversation-end" ref={endRef} />
        </div>
      </div>
      <div className="border-t border-border px-8 py-4" data-slot="chat-input-area">
        <div className="mx-auto w-full max-w-3xl">
          <ChatInput />
        </div>
      </div>
    </div>
  );
}
