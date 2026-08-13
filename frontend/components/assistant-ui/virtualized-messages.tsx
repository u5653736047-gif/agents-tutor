"use client";

// assistant-ui 接入(T13):长会话虚拟化(>50 条)——headless 组合。
// 库提供的稳定形状:unstable_useThreadMessageIds 拿消息 id 序列,
// ThreadPrimitive.Unstable_MessageById 按 id 渲染单条(官方注释即为
// 虚拟化/自定义列表的组合形态);虚拟化参数照搬现版旧路径
// (conversation-panel.tsx L553-565:estimateSize 96 / overscan 8 /
// gap 16,getItemKey 用消息 id),前后 spacer 补足未渲染行高度,
// measureElement 动态校正行高——DOM 消息行数有界(≤视口+overscan+2)。
//
// 与旧路径的边界一致:>50 条启用;≤50 条由调用方回退全量 Messages
// (短会话虚拟化无收益且动态测量有抖动,阈值两侧行为与旧路径相同)。

import {
  ThreadPrimitive,
  unstable_useThreadMessageIds,
} from "@assistant-ui/react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { ComponentType, RefObject } from "react";

type MessageComponents = {
  UserMessage: ComponentType;
  AssistantMessage: ComponentType;
};

export type VirtualizedMessagesProps = {
  /** Thread Viewport 的滚动容器引用(virtualizer 的 getScrollElement) */
  viewportRef: RefObject<HTMLDivElement | null>;
  components: MessageComponents;
};

export function VirtualizedMessages({
  viewportRef,
  components,
}: VirtualizedMessagesProps) {
  const messageIds = unstable_useThreadMessageIds();
  const virtualizer = useVirtualizer<HTMLDivElement, HTMLDivElement>({
    count: messageIds.length,
    getScrollElement: () => viewportRef.current,
    // 文本行高不定,96px 仅作首屏估算,measureElement 挂载后按实际校正
    estimateSize: () => 96,
    // 消息 id 即稳定 key(转换器保证 id 稳定:created_at 或位置 id)
    getItemKey: (index) => messageIds[index] ?? `msg-${index}`,
    overscan: 8,
    // 与内容列 flex gap-4 一致:虚拟位置按「行高 + gap」累加,否则长列表
    // 底部累积偏差,滚动不到最后一条(旧路径同款修正)
    gap: 16,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const totalSize = virtualizer.getTotalSize();

  return (
    <>
      {/* 前置 spacer:把首行推到估算位置 */}
      {virtualItems.length > 0 ? (
        <div style={{ height: virtualItems[0]?.start ?? 0 }} />
      ) : null}
      {virtualItems.map((item) => {
        const messageId = messageIds[item.index];
        if (!messageId) {
          return null;
        }
        return (
          <div
            data-index={item.index}
            key={item.key}
            ref={virtualizer.measureElement}
          >
            <ThreadPrimitive.Unstable_MessageById
              components={components}
              messageId={messageId}
            />
          </div>
        );
      })}
      {/* 尾部 spacer:补足未渲染行的估算高度,滚动条总高与全量一致 */}
      {virtualItems.length > 0 ? (
        <div
          style={{
            height: Math.max(
              0,
              totalSize - (virtualItems[virtualItems.length - 1]?.end ?? 0),
            ),
          }}
        />
      ) : null}
    </>
  );
}
