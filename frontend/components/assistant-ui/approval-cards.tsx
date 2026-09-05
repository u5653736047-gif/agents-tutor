"use client";

// assistant-ui 接入(T9):审批双通道挂接。
//
// 位置语义:旧路径把 HandoffCard / TerminalApprovalCard 渲染在消息列表尾部、
// 输入区之前(conversation-panel.tsx L669-696),本组件在新路径复刻同一位置
// (Viewport 内、Messages 之后)——审批不是消息内容,而是「当前轮的待决断点」,
// 挂在消息流尾部而非消息内部。
//
// 为什么不经转换器做 data part:pendingHandoff 来自 ChatResponse 同步通道
// (无对应 SSE 事件),pendingToolApproval 虽由 approval_required 事件携带,
// 但权威状态在 store(interrupt_id 防陈旧、409 兜底刷新等逻辑都在
// chat-store 的 decide* action 里)。两张卡片保持 props 驱动零改动复用,
// 本组件只做 store 订阅与 props 映射——错误映射(session_busy /
// tool_approval_not_pending)逐项照抄旧面板。

import { HandoffCard } from "@/components/handoff-card";
import { TerminalApprovalCard } from "@/components/terminal-approval-card";
import { useChatStore } from "@/stores/chat-store";

export function ApprovalCards() {
  const decideHandoff = useChatStore((state) => state.decideHandoff);
  const decideToolApproval = useChatStore((state) => state.decideToolApproval);
  const isDecidingHandoff = useChatStore((state) => state.isDecidingHandoff);
  const isDecidingToolApproval = useChatStore(
    (state) => state.isDecidingToolApproval,
  );
  const pendingHandoff = useChatStore((state) => state.pendingHandoff);
  const pendingToolApproval = useChatStore((state) => state.pendingToolApproval);
  const requestError = useChatStore((state) => state.requestError);

  return (
    <>
      <HandoffCard
        errorMessage={
          requestError?.code === "session_busy" ? requestError.message : null
        }
        isDeciding={isDecidingHandoff}
        onDecide={(action, modifications) =>
          void decideHandoff(action, modifications)
        }
        pending={pendingHandoff}
      />
      <TerminalApprovalCard
        errorMessage={
          requestError?.code === "session_busy" ||
          requestError?.code === "tool_approval_not_pending"
            ? requestError.message
            : null
        }
        isDeciding={isDecidingToolApproval}
        onDecide={(action) => void decideToolApproval(action)}
        pending={pendingToolApproval}
      />
    </>
  );
}
