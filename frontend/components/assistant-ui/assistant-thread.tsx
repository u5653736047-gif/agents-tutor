"use client";

// assistant-ui 接入(T4):新渲染路径的 Thread 壳。
// 布局对齐旧 ConversationPanel(conversation-panel.tsx L613-724):
// 可滚消息区(aria-live + data-slot="message-list" 锚点保留)+ 底部输入区。
// 滚动:ThreadPrimitive.Viewport 自带 autoScroll(贴底跟随/上翻暂停,
// T10 对照 lib/scroll-follow.ts 语义逐项验收);虚拟化 parity 属 T13。
// 输入区一期原样复用 ChatInput(直连 store 提交,与 Thread 读同一 store,
// 天然一致;附件/slash-commands/停止按钮零移植成本,原生 Composer 属 T14)。

import { ThreadPrimitive } from "@assistant-ui/react";

import { ChatInput } from "@/components/chat-input";

import { useChatStore } from "@/stores/chat-store";

import { ApprovalCards } from "./approval-cards";
import { AssistantMessage, UserMessage } from "./assistant-message";
import { AssistantRuntimeBridge } from "./runtime-provider";
import { RunErrorBlocks } from "./run-error-blocks";

// 消息组件分派表(模块级常量:ThreadPrimitive.Messages 的 memo 按引用比较)
const MESSAGE_COMPONENTS = {
  UserMessage,
  AssistantMessage,
} as const;

// T10:流式状态 sr-only 播报行——复刻旧面板(conversation-panel.tsx L298-302):
// 位于 aria-live 区域内,进入/离开流式由读屏自然播报;结束不播报(live
// region 内容清空即表示结束)。isStreaming 优先于 isSending(两者理论互斥)。
function LiveStatusLine() {
  const isSending = useChatStore((state) => state.isSending);
  const isStreaming = useChatStore((state) => state.isStreaming);
  if (!isStreaming && !isSending) {
    return null;
  }
  return (
    <p className="sr-only" data-slot="live-status">
      {isStreaming ? "助手正在生成回答…" : "正在发送…"}
    </p>
  );
}

export default function AssistantThread() {
  return (
    <AssistantRuntimeBridge>
      <div className="flex min-h-0 flex-1 flex-col" data-slot="assistant-thread">
        <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
          <ThreadPrimitive.Viewport
            aria-live="polite"
            className="min-h-0 flex-1 overflow-y-auto"
            data-slot="message-list"
          >
            {/* T10:aria-live 区域内的状态播报行(读屏用户可感知生成进行中) */}
            <LiveStatusLine />
            <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 px-4 py-8 md:px-8">
              <ThreadPrimitive.Messages components={MESSAGE_COMPONENTS} />
              {/* T9:审批卡片——消息列表尾部、与旧路径同一位置语义;无待决
                  项时两张卡片均零渲染(组件自身降级) */}
              <ApprovalCards />
              {/* T10:运行/网络错误块——审批卡片之后,与旧路径同一位置;
                  无错误时零渲染 */}
              <RunErrorBlocks />
            </div>
          </ThreadPrimitive.Viewport>
          <div
            className="bg-gradient-to-t from-background via-background to-transparent px-4 pb-4 pt-6 md:px-8"
            data-slot="chat-input-area"
          >
            <div className="mx-auto w-full max-w-4xl">
              <ChatInput />
            </div>
          </div>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeBridge>
  );
}
