"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { CircleAlert, LoaderCircle, WifiOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { AgentBadge } from "@/components/agent-badge";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import { ChatInput } from "@/components/chat-input";
import { CitationList } from "@/components/citation-list";
import {
  CollaborationPanel,
  type CollaborationPanelProps,
} from "@/components/collaboration-panel";
import { FeedbackButtons } from "@/components/feedback-buttons";
import { HandoffCard } from "@/components/handoff-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { AgentRole } from "@/lib/agent-roles";
import type { ApiClientError, ChatResponse, Message } from "@/lib/api-client";
// D7-T3:附件受控下载 URL(纯字符串拼接)。getFileUrl 是便捷入口,
// 见 api-client 注释;实际取文件必须带 X-User-Id 头(见 AttachmentPreview)。
import { DEMO_USER_ID, getFileUrl } from "@/lib/api-client";
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

// D7-T3:附件大小展示(可选增强)——B/KB/MB 简易格式化,零依赖。
// 非图片附件链接上顺带展示体积,便于用户判断下载内容大小。
function formatAttachmentSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

// D7-T3 review blocking 修复:直链 <img>/<a> 指向 /files/{file_id} 无法
// 携带 X-User-Id 自定义头,而后端下载端点按当前请求头消毒出 user_key
// 定位文件(无头 → anonymous 目录),真实文件在 demo-user/ 目录下必然
// 404 破图。改为 fetch 带鉴权头拉 Blob → objectURL:
//   - 图片:objectURL 内联预览,点击新标签打开(objectURL 同源可开);
//   - PDF/其它:objectURL 下载链接(download 属性用原始文件名);
//   - 加载中显示骨架占位,失败显示降级文案(诚实降级,不破图)。
// effect 内 await fetch 后 setState(异步回调,set-state-in-effect 只拦
// 同步路径),SSR 首帧 url=null 渲染占位,无 hydration mismatch。
function AttachmentPreview({ attachment }: { attachment: NonNullable<Message["attachments"]>[number] }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const isImage = attachment.content_type?.startsWith("image/") ?? false;

  useEffect(() => {
    let ignore = false;
    let objectUrl: string | null = null;
    async function load() {
      try {
        const response = await fetch(getFileUrl(attachment.file_id), {
          headers: { "X-User-Id": DEMO_USER_ID },
        });
        if (!response.ok) {
          throw new Error(`file fetch failed: ${response.status}`);
        }
        const blob = await response.blob();
        if (!ignore) {
          objectUrl = URL.createObjectURL(blob);
          setUrl(objectUrl);
        }
      } catch {
        if (!ignore) {
          setFailed(true);
        }
      }
    }
    void load();
    return () => {
      ignore = true;
      // review should-fix:虚拟化滚动反复挂载/卸载,必须 revoke objectURL
      // 防 Blob(上限 10MB)累积泄漏;url 状态在卸载后不再使用。
      if (objectUrl !== null) {
        URL.revokeObjectURL(objectUrl);
      }
    };
  }, [attachment.file_id]);

  if (failed) {
    return (
      <p className="text-caption text-destructive" data-slot="attachment-failed">
        附件加载失败
      </p>
    );
  }
  if (url === null) {
    // 加载占位:与图片缩略图高度接近,避免布局跳动
    return <Skeleton className={isImage ? "h-40 w-40" : "h-6 w-48"} />;
  }
  return isImage ? (
    <a
      className="block w-fit"
      href={url}
      rel="noreferrer"
      target="_blank"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        alt={attachment.name}
        className="max-h-40 rounded-md border border-border"
        data-slot="attachment-image"
        src={url}
      />
    </a>
  ) : (
    <a
      className="w-fit max-w-full truncate text-primary-foreground underline underline-offset-2"
      data-slot="attachment-link"
      download={attachment.name}
      href={url}
    >
      {attachment.name}
      <span className="ml-1 text-caption opacity-70">
        {formatAttachmentSize(attachment.size)}
      </span>
    </a>
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
  // D5-T2:消息进入动画开关——仅全量路径的最后一条(新消息)为 true,
  // 虚拟化窗口行与历史消息为 false(滚动挂载不闪动,见 ConversationContent 注释)
  animate?: boolean;
  // 虚拟化路径:行在列表中的真实索引,渲染为 data-index 供 measureElement 定位
  dataIndex?: number;
  // 虚拟化路径:useVirtualizer 的 measureElement(ref 回调),动态校正行高
  measureRef?: (node: HTMLElement | null) => void;
  // D6-T2:反馈挂载参数——assistant 行在气泡下方渲染 FeedbackButtons;
  // 反馈提交失败由 FeedbackButtons 内部错误行呈现,不进入主流程错误。
  // 两者同时提供才渲染(既有调用不传,行为不变)。
  feedbackSessionId?: string;
  onFeedback?: (rating: "up" | "down", comment?: string) => Promise<void> | void;
};

export function MessageRow({
  message,
  index,
  animate = false,
  dataIndex,
  measureRef,
  feedbackSessionId,
  onFeedback,
}: MessageRowProps) {
  const isUser = message.role === "user";
  // 虚拟化行必须带 data-index(measureElement 依赖);全量路径不渲染
  const rowIndex = dataIndex ?? index;

  return (
    <article
      // D5-T2:消息进入动画(tw-animate-css:淡入 + 底部轻滑入,时长/缓动
      // 对齐 D5-T1 tokens)。仅新消息(animate=true)带类——CSS 动画只在
      // 挂载时播放一次,历史/虚拟化行挂载时若带类会在滚动浏览时逐行闪动;
      // reduced-motion 偏好由 globals.css 的全局媒体查询统一关闭。
      className={
        // D6-T2:assistant 行改为纵向堆叠(flex-col),气泡下方容纳
        // 反馈按钮;布局等价(气泡仍按内容宽度、受 max-w-[80%] 约束)
        (isUser ? "flex justify-end" : "flex flex-col items-start") +
        (animate
          ? " animate-in fade-in-0 slide-in-from-bottom-1 duration-[var(--app-duration-normal)] ease-[var(--app-ease-out)]"
          : "")
      }
      data-index={measureRef ? rowIndex : undefined}
      data-message-role={message.role}
      ref={measureRef}
    >
      <div
        className={
          isUser
            ? "max-w-[85%] rounded-2xl rounded-br-md bg-primary px-4 py-3 text-body text-primary-foreground shadow-sm md:max-w-[75%]"
            : "max-w-[90%] rounded-2xl rounded-bl-md border border-border/70 bg-card/80 px-5 py-4 text-body text-foreground shadow-sm md:max-w-[85%]"
        }
      >
        {!isUser ? <AssistantBadge agent={message.agent} /> : null}
        {isUser ? (
          <>
            <p className="whitespace-pre-wrap">{message.content}</p>
            {/* D7-T3:附件区——仅用户消息且携带附件时渲染(文本之后);
                无附件零渲染(data-slot 不出现):历史消息后端映射
                attachments=null,自然降级;助手消息理论上不携带附件,
                防御性不渲染(仅用户侧)。 */}
            {message.attachments && message.attachments.length > 0 ? (
              <div
                className="mt-3 flex flex-col items-start gap-2"
                data-slot="message-attachments"
              >
                {message.attachments.map((attachment) => (
                  <AttachmentPreview
                    attachment={attachment}
                    key={attachment.file_id}
                  />
                ))}
              </div>
            ) : null}
          </>
        ) : (
          <div className="mt-2">
            <AssistantMarkdown content={message.content} />
          </div>
        )}
      </div>
      {/* D6-T2:反馈按钮——仅 assistant 行、且调用方接线(会话 + 回调)
          时渲染;messageId 用 created_at(权威历史消息才有,乐观/流式
          消息为 undefined 时不带,契约 message_id 可空) */}
      {!isUser && feedbackSessionId && onFeedback ? (
        <FeedbackButtons
          messageId={message.created_at ?? undefined}
          onFeedback={onFeedback}
          sessionId={feedbackSessionId}
        />
      ) : null}
    </article>
  );
}

type ConversationContentProps = {
  collaboration?: CollaborationPanelProps;
  // UX-20260807#2:切换会话拉历史期间(isLoadingMessages && 无消息)渲染
  // 加载骨架,区分「正在加载」与「会话是空的」。可选:既有调用不传时
  // 零渲染,行为不变。
  isLoadingMessages?: boolean;
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
  // D6-T2:反馈挂载参数(透传给每条消息行)——非空时 assistant 行渲染
  // 反馈按钮;未提供(既有调用/测试)时零渲染,行为不变
  feedbackSessionId?: string;
  onFeedback?: (rating: "up" | "down", comment?: string) => Promise<void> | void;
};

export function ConversationContent({
  collaboration,
  isLoadingMessages = false,
  isSending,
  isStreaming,
  messages,
  onRetry,
  requestError,
  runError,
  streamingAgent,
  streamingMessage,
  virtualItems,
  feedbackSessionId,
  onFeedback,
}: ConversationContentProps) {
  // D2-T5:网络失败/超时(code===null)预设,供消息流下方的网络错误块使用
  const networkPreset = errorMessageFor(null);
  const runErrorPreset = runError ? errorMessageFor(runError.error_code) : null;

  return (
    <>
      {/* D5-T5:aria-live 状态播报——D5-T3 骨架替换纯文本占位后,读屏用户
          无法感知生成进行中;此 sr-only 文本位于消息流区(data-slot=
          "message-list")的 aria-live="polite" 区域内,进入/离开时由读屏
          自然播报(流式进行中/发送中状态文案,见下方 JSX)。结束不播报:
          live region 内容清空即表示结束,读屏可感知(「完成」播报需在
          渲染期比较 isStreaming 变化,D4-T5 教训:渲染期访问 ref 被
          react-hooks lint 拦截,放弃)。isStreaming 优先于 isSending
          (流式期间两者理论上互斥)。 */}
      {isStreaming || isSending ? (
        <p className="sr-only" data-slot="live-status">
          {isStreaming ? "助手正在生成回答…" : "正在发送…"}
        </p>
      ) : null}

      {/* UX-20260807#2:切换会话拉历史期间的加载骨架——复用 isSending 的
          message-skeleton 结构(徽章占位 + 两行文本占位),仅 isLoadingMessages
          且无消息时渲染,区分「正在加载」与「会话是空的」;加载结束后由
          消息行或既有空态接替。data-slot 与发送骨架区分,测试可独立断言。 */}
      {isLoadingMessages && messages.length === 0
        ? [0, 1, 2].map((index) => (
            <article
              aria-hidden
              className="flex justify-start"
              data-slot="history-skeleton"
              key={index}
            >
              <div className="max-w-[90%] rounded-2xl rounded-bl-md border border-border/70 bg-card/80 px-5 py-4 shadow-sm md:max-w-[85%]">
                <div className="flex items-center gap-2">
                  <Skeleton className="size-5 rounded-full" />
                  <Skeleton className="h-4 w-16" />
                </div>
                <div className="mt-3 space-y-2">
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-4/5" />
                </div>
              </div>
            </article>
          ))
        : null}

      {/* D4-T8:未启用虚拟化时全量渲染消息行(短会话既有行为);
          启用时消息行由 ConversationPanel 虚拟渲染,此处仅渲染尾部块 */}
      {virtualItems == null
        ? messages.map((message, index) => (
            <MessageRow
              // D5-T2:仅最后一条(新消息)带进入动画;历史消息不带,
              // 避免虚拟化/滚动挂载时每行重播动画闪动
              animate={index === messages.length - 1}
              feedbackSessionId={feedbackSessionId}
              key={message.created_at ?? `${message.role}-${index}`}
              message={message}
              index={index}
              onFeedback={onFeedback}
            />
          ))
        : null}

      {/* 当前轮执行轨迹与回答属于同一个视觉单元：在历史/用户消息后、
          流式回答前展示。只在已有过程数据时挂载，普通闲置会话不占位。 */}
      {collaboration &&
      (collaboration.events.length > 0 || collaboration.taskPlan !== null) ? (
        <CollaborationPanel {...collaboration} />
      ) : null}

      {/* 流式气泡:isStreaming 期间渲染,或异常中断后保留已收到内容时继续展示 */}
      {isStreaming || streamingMessage ? (
        <article
          // D5-T2:流式气泡挂载即新消息,带淡入动画(仅 fade-in,不用 slide——
          // 内容逐字渲染时位移动画会与追加叠加显得跳动);时长对齐 D5-T1 tokens。
          className="flex justify-start animate-in fade-in-0 duration-[var(--app-duration-normal)] ease-[var(--app-ease-out)]"
          data-message-role="assistant"
          data-slot="streaming-message"
        >
          <div className="max-w-[90%] rounded-2xl rounded-bl-md border border-border/70 bg-card/80 px-5 py-4 text-body text-foreground shadow-sm md:max-w-[85%]">
            <AssistantBadge agent={streamingAgent} />
            <div className="mt-2">
              <AssistantMarkdown content={streamingMessage?.content ?? ""} />
            </div>
            {isStreaming ? (
              // D5-T3:渐进式衔接——首事件前(streamingMessage 为 null)气泡
              // 内显示两行灰条骨架(data-slot="streaming-skeleton",区别于
              // 同步路径的 message-skeleton);store 的 streamingMessage
              // 收到首个增量后组件重渲染,骨架行自然消失切换真实内容
              // (依赖 D1-T2 流式内容衔接,无额外切换逻辑)。骨架自身
              // animate-pulse 呼吸,不叠加进入动画。有内容后保留既有
              // LoaderCircle「正在生成…」指示。review nit:以 null 判定
              // 「首事件前」,空内容回答不误判为骨架。
              streamingMessage === null ? (
                <div
                  aria-hidden
                  className="mt-2 space-y-2"
                  data-slot="streaming-skeleton"
                >
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-4/5" />
                </div>
              ) : (
                // D5-T5:视觉指示 aria-hidden——「正在生成…」是视觉占位,
                // 进行中状态已由 sr-only live-status 播报,避免读屏重复朗读
                <div
                  aria-hidden
                  className="mt-2 flex items-center gap-2 text-caption text-muted-foreground"
                >
                  <LoaderCircle aria-hidden className="size-4 animate-spin" />
                  正在生成…
                </div>
              )
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

      {/* D5-T3:同步路径发送骨架——结构与助手气泡对齐(徽章占位 + 两行
          文本占位),渐进式:发送中显示骨架,流式首事件到达后切换真实
          气泡(下方 streaming-message 块);骨架自身 animate-pulse 呼吸,
          不再叠加 D5-T2 进入动画(两种动画叠加无意义且费电)。
          D5-T5:骨架是视觉占位,aria-hidden 避免读屏朗读骨架噪音
          (进行中状态由上方 sr-only live-status 播报)。 */}
      {isSending ? (
        <article
          aria-hidden
          className="flex justify-start"
          data-slot="message-skeleton"
        >
          <div className="max-w-[90%] rounded-2xl rounded-bl-md border border-border/70 bg-card/80 px-5 py-4 shadow-sm md:max-w-[85%]">
            <div className="flex items-center gap-2">
              <Skeleton className="size-5 rounded-full" />
              <Skeleton className="h-4 w-16" />
            </div>
            <div className="mt-3 space-y-2">
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-4/5" />
            </div>
          </div>
        </article>
      ) : null}
    </>
  );
}

export function ConversationPanel() {
  const currentAgent = useChatStore((state) => state.currentAgent);
  const currentSessionId = useChatStore((state) => state.currentSessionId);
  // D2-T3:审批卡片数据(待审批项、决策中标记、决策错误、决策动作)
  const decideHandoff = useChatStore((state) => state.decideHandoff);
  const isDecidingHandoff = useChatStore((state) => state.isDecidingHandoff);
  // UX-20260807#2:拉历史加载态——此前无组件订阅(「死状态」),切换会话
  // 期间消息区空白;现在驱动 ConversationContent 的加载骨架。
  const isLoadingMessages = useChatStore((state) => state.isLoadingMessages);
  const isSending = useChatStore((state) => state.isSending);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const events = useChatStore((state) => state.events);
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
  // D6-T2:反馈提交 action——与主对话流程解耦(不写 requestError),
  // 失败由 FeedbackButtons 组件内错误行呈现
  const submitFeedback = useChatStore((state) => state.submitFeedback);
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

  // D6-T2:反馈提交回调——闭包绑定当前会话(无会话时不渲染反馈按钮,
  // 见 MessageRow 的 feedbackSessionId 守卫);messageId 由 MessageRow
  // 从消息 created_at 注入。store action 不写 requestError,失败由
  // FeedbackButtons 内部错误行呈现(任务「失败静默降级,不阻塞对话」)。
  const handleFeedback = currentSessionId
    ? (rating: "up" | "down", comment?: string) =>
        submitFeedback({ rating, sessionId: currentSessionId, comment })
    : undefined;

  useEffect(() => {
    // D4-T8:用户上翻浏览(followBottom=false)时不打扰,
    // 回到底部后由 onScroll 恢复跟随
    if (followBottom.current) {
      endRef.current?.scrollIntoView({ block: "end" });
    }
  }, [
    isDecidingHandoff,
    isSending,
    isStreaming,
    events,
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
        // D5-T5:aria-live="polite"——消息流更新(新消息追加/流式内容逐段
        // 进入)时读屏播报新增内容;sr-only live-status 状态行位于区域内,
        // 生成中/发送中状态自然播报。取舍:理想做法是 live region 与可聚焦
        // 控件分离,但消息行含按钮(重试等)且更新频繁,整体区域播报增量
        // 内容、噪音可控,保持简单。
        aria-live="polite"
        className="min-h-0 flex-1 overflow-y-auto"
        data-slot="message-list"
        onScroll={handleScroll}
        ref={parentRef}
      >
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 px-4 py-8 md:px-8">
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
                    feedbackSessionId={currentSessionId ?? undefined}
                    index={item.index}
                    key={message.created_at ?? `msg-${item.index}`}
                    measureRef={virtualizer.measureElement}
                    message={message}
                    onFeedback={handleFeedback}
                  />
                );
              })
            : null}
          <ConversationContent
            collaboration={{ currentAgent, events, taskPlan, taskResults }}
            feedbackSessionId={currentSessionId ?? undefined}
            isLoadingMessages={isLoadingMessages}
            isSending={isSending}
            isStreaming={isStreaming}
            messages={messages}
            onFeedback={handleFeedback}
            onRetry={
              lastSentMessage ? () => void retryLastMessage() : undefined
            }
            requestError={requestError}
            runError={runError ?? null}
            streamingAgent={streamingAgent}
            streamingMessage={streamingMessage}
            virtualItems={isVirtualized ? virtualItems : null}
          />
          {/* D2-T3:审批卡片——消息之后、输入区之前;错误文案只映射
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
              最后一轮回答,跟随该轮回答一起展示;store
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
      <div
        className="bg-gradient-to-t from-background via-background to-transparent px-4 pb-4 pt-6 md:px-8"
        data-slot="chat-input-area"
      >
        <div className="mx-auto w-full max-w-4xl">
          <ChatInput />
        </div>
      </div>
    </div>
  );
}
