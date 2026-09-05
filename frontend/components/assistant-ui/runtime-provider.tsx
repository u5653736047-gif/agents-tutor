"use client";

// assistant-ui 接入(T4):chat-store → ExternalStoreRuntime 桥接层。
//
// 设计要点:
// - store 零改动红线:本组件只读订阅 chat-store,经 T3 纯函数转换器产出
//   ThreadMessageLike[],再以恒等 convertMessage 交给 runtime(消息已是
//   目标形状,无需逐条再转换;useExternalMessageConverter 内部 WeakMap
//   缓存以消息对象为键,转换器的引用相等 memo 在此直接兑现为重渲染跳过);
// - handler 映射:onNew → streamSendMessage(仅文本——附件/斜杠命令由
//   复用的 ChatInput 直连 store 提交,不经 runtime);onCancel →
//   cancelStreaming;onReload → retryLastMessage;isSendDisabled 对齐
//   store 的 pendingToolApproval 发送闸门(chat-store.ts L832-834);
// - 不提供 onEdit / setMessages:编辑/分支 UI 自动隐藏(最小能力面);
// - handler 全部 useCallback 稳定引用,避免 runtime setAdapter 抖动。
//
// 双气泡检查点(计划 T4 降级方案):在飞气泡由转换器按 events +
// streamingMessage 合成(messages 数组驱动),ExternalStoreRuntime 自身
// 不合成乐观气泡,理论无双气泡;若实测出现(isRunning 启发式与在飞消息
// 叠加),降级为 isRunning: false 并仅用 isSendDisabled 门控输入。

import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
} from "@assistant-ui/react";
import { useCallback, type PropsWithChildren } from "react";

import { type ConvertedMessage } from "@/lib/assistant/message-converter";
import { useChatStore } from "@/stores/chat-store";

import {
  attachmentFromPart,
  createAttachmentAdapter,
} from "./attachment-adapter";
import { useThrottledConversation } from "./use-throttled-conversion";

// 恒等转换:消息已在转换器中成形;模块级常量保证引用稳定
// (convertMessage 变化会击穿 runtime 内部的消息缓存)
const identityConverter = (message: ConvertedMessage) => message;

// T14:附件适配器(模块级常量,发送时上传语义);T14 子开关关闭时
// (旧 ChatInput 输入)适配器不被任何附件 UI 触达,零行为变化
const attachmentAdapter = createAttachmentAdapter();

export function AssistantRuntimeBridge({ children }: PropsWithChildren) {
  // T12:转换输入经帧级合并(≤30fps),store 每 token 的高频 setState
  // 在此降频;isRunning/isSendDisabled 同切片派生,语义不变
  const { converted, isRunning, isSendDisabled } = useThrottledConversation();
  const streamSendMessage = useChatStore((state) => state.streamSendMessage);
  const cancelStreaming = useChatStore((state) => state.cancelStreaming);
  const retryLastMessage = useChatStore((state) => state.retryLastMessage);

  const onNew = useCallback(
    async (message: AppendMessage) => {
      // 仅文本:复用的 ChatInput 直连 store 提交附件/斜杠命令,经 runtime
      // 的 onNew 只会来自 assistant-ui 自身 Composer(T14 前不挂载)
      const text = message.content
        .filter((part) => part.type === "text")
        .map((part) => part.text)
        .join("\n")
        .trim();
      // T14:附件回执还原(data-attachment part → 契约 Attachment;
      // 宽容读取,非法项跳过)——经原生 Composer 提交才有附件
      const attachments = (message.attachments ?? [])
        .map((attachment) => attachmentFromPart(attachment))
        .filter((attachment) => attachment !== null);
      if (!text && attachments.length === 0) {
        return;
      }
      await streamSendMessage(
        text,
        attachments.length > 0 ? attachments : undefined,
      );
    },
    [streamSendMessage],
  );
  const onCancel = useCallback(async () => {
    // runtime 契约要求 Promise 返回;store 的取消是同步 abort,直接包裹
    cancelStreaming();
  }, [cancelStreaming]);
  const onReload = useCallback(async () => {
    await retryLastMessage();
  }, [retryLastMessage]);

  const runtime = useExternalStoreRuntime<ConvertedMessage>({
    messages: converted,
    convertMessage: identityConverter,
    isRunning,
    isSendDisabled,
    onNew,
    onCancel,
    onReload,
    adapters: { attachments: attachmentAdapter },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {children}
    </AssistantRuntimeProvider>
  );
}
