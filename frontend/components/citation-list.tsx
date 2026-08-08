"use client";

// D3-T4:引用卡片列表。
// 展示本轮回答携带的检索引用(ChatResponse.references,经 store 订阅后
// 由 ConversationPanel 以 props 传入),纯展示组件、自身不订阅 store,
// 便于 SSR 渲染与组件测试:
//   1. 降级红线:citations 为 null / 空数组时零渲染(return null),不
//      占位、不报错——历史轮次、无检索轮次的回答都没有引用,UI 必须
//      在字段缺失时静默降级;
//   2. 每条引用:编号 + 来源标识 + 「查看」按钮,点击展开该条引用的
//      字段详情(document_id / source / page / chunk_id 文本信息);
//   3. 「查看原文」接口 core 未提供,展开只展示字段文本(交互与接口
//      均预留,后续接入原文跳转时替换展开内容即可)。
import { useState } from "react";

import type { components } from "@/contracts/api.generated";

type Citation = components["schemas"]["Citation"];

export type CitationListProps = {
  citations: Citation[] | null;
};

export function CitationList({ citations }: CitationListProps) {
  // 展开状态:记录当前展开的引用下标(null = 全部收起)。展开详情只
  // 展示字段文本(「查看原文」接口预留,见组件头注释)。
  // 注意:hooks 必须在组件顶层无条件调用——若把 useState 放在下方
  // early return 之后,references 从 null 变为数组时同一实例的 hook
  // 数量会从 0 变 1,违反 React hooks 规则(eslint react-hooks 也会拦截)。
  const [expandedIndex, setExpandedIndex] = useState<number | null>(null);

  // 降级红线:null(后端未携带引用)或空数组均零渲染——引用是可选
  // 元数据,缺失时必须静默降级,不得渲染占位提示或报错。
  if (citations == null || citations.length === 0) {
    return null;
  }

  return (
    <section
      aria-label="回答引用"
      className="rounded-lg border border-border bg-muted/50 px-4 py-3 text-caption"
      data-slot="citation-list"
    >
      <h3 className="font-medium text-muted-foreground">引用来源</h3>
      <ol className="mt-2 flex flex-col gap-2">
        {citations.map((citation, index) => {
          const expanded = expandedIndex === index;
          return (
            <li
              className="rounded-md border border-border bg-card px-3 py-2"
              data-slot="citation-item"
              key={`${citation.chunk_id}-${index}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate text-foreground">
                  <span className="mr-1.5 text-muted-foreground">[{index + 1}]</span>
                  {citation.source}
                </span>
                <button
                  aria-expanded={expanded}
                  className="shrink-0 rounded border border-border px-2 py-0.5 font-medium text-primary hover:bg-muted"
                  data-slot="citation-toggle"
                  onClick={() => setExpandedIndex(expanded ? null : index)}
                  type="button"
                >
                  {expanded ? "收起" : "查看"}
                </button>
              </div>
              {/* 展开详情:只展示引用字段文本,不依赖后端原文接口 */}
              {expanded ? (
                <dl
                  className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground"
                  data-slot="citation-detail"
                >
                  <dt>文档</dt>
                  <dd className="break-all">{citation.document_id}</dd>
                  <dt>来源</dt>
                  <dd className="break-all">{citation.source}</dd>
                  <dt>页码</dt>
                  <dd>{citation.page ?? "—"}</dd>
                  <dt>分块</dt>
                  <dd className="break-all">{citation.chunk_id}</dd>
                </dl>
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
