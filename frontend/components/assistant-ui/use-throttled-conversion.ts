"use client";

// assistant-ui 接入(T12):桥层帧级合并——store 高频 setState(流式每 token
// 一次)到 runtime 通知之间的唯一合法降频点(store 零改动红线,降频只能
// 发生在只读适配层)。
//
// 机制(领先 + 尾沿节流):
//   - 距上次冲刷 ≥33ms(30fps 一帧)的变更立即冲刷(领先沿,首 token
//     感知不被延迟);
//   - 帧内的连续变更合并为一次尾沿冲刷——尾沿永远携带最新状态,不丢终态
//     (isStreaming=false 的收尾态必然落达);
//   - 冲刷经 startTransition 降级:Thread 重渲染是可中断的低优先更新,
//     流式期间输入框键入保持高优先(React 19 并发语义);
//   - 引用守卫:所摘切片字段引用全等时返回上一切片对象,下游 useMemo
//     与 runtime 内部缓存(以消息对象为键)直接命中,无意义重算归零。

import { startTransition, useEffect, useMemo, useRef, useState } from "react";

import {
  convertConversationToThreadMessages,
  type ConversationSlice,
} from "@/lib/assistant/message-converter";
import { useChatStore, type ChatStore } from "@/stores/chat-store";

/** 帧级合并间隔(30fps 一帧;对流式 token 流低于感知阈值) */
export const COALESCE_FRAME_MS = 33;

/** 帧级合并器(纯逻辑,定时器/时钟可注入,单测友好)。 */
export function createFrameCoalescer(
  flush: () => void,
  frameMs: number = COALESCE_FRAME_MS,
  now: () => number = Date.now,
  schedule: (callback: () => void, ms: number) => unknown = (callback, ms) =>
    setTimeout(callback, ms),
  cancel: (handle: unknown) => void = (handle) => clearTimeout(handle as never),
): { notify: () => void; dispose: () => void } {
  let lastFlush = Number.NEGATIVE_INFINITY;
  let pendingHandle: unknown = null;

  const runFlush = () => {
    pendingHandle = null;
    lastFlush = now();
    flush();
  };

  return {
    notify: () => {
      const elapsed = now() - lastFlush;
      if (elapsed >= frameMs) {
        // 领先沿:空闲后第一个变更立即生效
        runFlush();
        return;
      }
      if (pendingHandle === null) {
        pendingHandle = schedule(runFlush, frameMs - elapsed);
      }
    },
    dispose: () => {
      if (pendingHandle !== null) {
        cancel(pendingHandle);
        pendingHandle = null;
      }
    },
  };
}

type PickedSlice = ConversationSlice & {
  isSending: boolean;
  pendingToolApproval: ChatStore["pendingToolApproval"];
};

function pickSlice(state: ChatStore): PickedSlice {
  return {
    events: state.events,
    isSending: state.isSending,
    isStreaming: state.isStreaming,
    messages: state.messages,
    pendingToolApproval: state.pendingToolApproval,
    references: state.references,
    streamingAgent: state.streamingAgent,
    streamingMessage: state.streamingMessage,
    taskPlan: state.taskPlan,
    taskResults: state.taskResults,
  };
}

function sameSliceReferences(a: PickedSlice, b: PickedSlice): boolean {
  return (Object.keys(a) as (keyof PickedSlice)[]).every(
    (key) => a[key] === b[key],
  );
}

export type ThrottledConversation = {
  converted: ReturnType<typeof convertConversationToThreadMessages>;
  isRunning: boolean;
  isSendDisabled: boolean;
};

/**
 * 订阅 chat-store 并以 ≤30fps 的合并频率输出转换结果。
 * 首帧取当前状态(SSR 初始态/挂载即一致),此后 store 每次变更经
 * 帧级合并器冲刷。
 */
export function useThrottledConversation(): ThrottledConversation {
  const [slice, setSlice] = useState<PickedSlice>(() =>
    pickSlice(useChatStore.getState()),
  );
  // 引用守卫需要跨渲染持有上一切片(ref 不触发渲染,冲刷时同步更新)
  const sliceRef = useRef(slice);

  useEffect(() => {
    const coalescer = createFrameCoalescer(() => {
      const next = pickSlice(useChatStore.getState());
      // 引用全等(无关字段变化,如侧栏会话列表刷新)不通知下游
      if (sameSliceReferences(sliceRef.current, next)) {
        return;
      }
      sliceRef.current = next;
      // startTransition 降级(计划 T12 明确项):Thread 重渲染是可被键入等
      // 高优更新中断的低优先更新,流式期间输入保持即时响应
      startTransition(() => {
        setSlice(next);
      });
    });
    const unsubscribe = useChatStore.subscribe(coalescer.notify);
    return () => {
      unsubscribe();
      coalescer.dispose();
    };
  }, []);
  return useMemo(
    () => ({
      converted: convertConversationToThreadMessages(slice),
      isRunning: slice.isStreaming || slice.isSending,
      isSendDisabled: slice.pendingToolApproval !== null,
    }),
    [slice],
  );
}
