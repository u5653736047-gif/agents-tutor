"use client";

// assistant-ui 接入(T7):助手消息 footer——引用列表 + 反馈按钮。
// 抽成纯展示组件(props 驱动、不读 runtime/store 上下文),便于
// renderToStaticMarkup 直接测试(与 collaboration-panel 测试先例一致);
// AssistantMessage 负责从 metadata.custom 与 chat-store 取值后传入。
//
// 降级语义(与旧路径逐项对齐):
//   - citations 为 null/空 → CitationList 零渲染(组件自身红线,D3-T4);
//   - 未注入反馈回调(无会话等)→ 不渲染反馈按钮(旧 MessageRow 同一条件);
//   - messageId 缺省(乐观/流式消息无 created_at)→ 传 undefined,契约可空。

import { CitationList } from "@/components/citation-list";
import { FeedbackButtons } from "@/components/feedback-buttons";
import type { components } from "@/contracts/api.generated";

type Citation = components["schemas"]["Citation"];

export type AssistantMessageFooterProps = {
  citations: Citation[] | null;
  feedbackSessionId?: string;
  messageId?: string;
  onFeedback?: (rating: "up" | "down", comment?: string) => Promise<void> | void;
};

export function AssistantMessageFooter({
  citations,
  feedbackSessionId,
  messageId,
  onFeedback,
}: AssistantMessageFooterProps) {
  const hasCitations = citations != null && citations.length > 0;
  const showFeedback = feedbackSessionId !== undefined && onFeedback !== undefined;
  if (!hasCitations && !showFeedback) {
    return null;
  }
  return (
    <div
      className="mt-2 flex flex-col items-start gap-2"
      data-slot="assistant-message-footer"
    >
      <CitationList citations={citations} />
      {feedbackSessionId && onFeedback ? (
        <FeedbackButtons
          messageId={messageId}
          onFeedback={onFeedback}
          sessionId={feedbackSessionId}
        />
      ) : null}
    </div>
  );
}
