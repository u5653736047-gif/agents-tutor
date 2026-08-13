"use client";

// assistant-ui 接入(T4/T7):text part 渲染器。
// 助手正文渲染委托现有 AssistantMarkdown(KaTeX/highlight/ErrorBoundary
// 与设计系统映射原样保留,单 Markdown 管线原则——不引入第二套包);
// 用户正文保持纯文本预格式(与旧 MessageRow 的 whitespace-pre-wrap 一致)。

import type { TextMessagePartProps } from "@assistant-ui/react";

import { AssistantMarkdown } from "@/components/assistant-markdown";

export function AssistantTextPart({ text }: TextMessagePartProps) {
  return <AssistantMarkdown content={text} />;
}

export function UserTextPart({ text }: TextMessagePartProps) {
  return <p className="whitespace-pre-wrap">{text}</p>;
}
