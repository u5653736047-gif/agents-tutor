"use client";

import { Component, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkMath from "remark-math";

// 数学公式渲染说明(KaTeX):
// - remark-math 把行内 $...$ 与块级 $$...$$ 解析成 math 节点;
// - rehype-katex 把这些节点渲染成 KaTeX 的 HTML(class="katex" / katex-display);
// - KaTeX 样式表(katex.min.css)在 app/globals.css 里通过 @import 全局引入,
//   负责公式字体与布局;组件内 import 会让 tsx 测试加载本模块时因 .css 扩展名报错,
//   因此不在这里引入;
// - 非法公式(如 KaTeX 无法解析的 TeX)由 rehype-katex 内部容错:记录告警后输出
//   带 katex-error 标记的错误样式,不会抛异常,页面不会因公式错误崩溃
//   (MarkdownErrorBoundary 仍负责其它渲染错误的兜底)。

type MarkdownErrorBoundaryProps = {
  children: ReactNode;
  content: string;
};

type MarkdownErrorBoundaryState = {
  hasError: boolean;
};

export class MarkdownErrorBoundary extends Component<
  MarkdownErrorBoundaryProps,
  MarkdownErrorBoundaryState
> {
  state: MarkdownErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): MarkdownErrorBoundaryState {
    return { hasError: true };
  }

  componentDidUpdate(previousProps: MarkdownErrorBoundaryProps) {
    if (this.state.hasError && previousProps.content !== this.props.content) {
      this.setState({ hasError: false });
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <p className="whitespace-pre-wrap" data-slot="markdown-fallback">
          {this.props.content}
        </p>
      );
    }

    return this.props.children;
  }
}

type AssistantMarkdownProps = {
  content: string;
};

export function AssistantMarkdown({ content }: AssistantMarkdownProps) {
  return (
    <MarkdownErrorBoundary content={content}>
      <ReactMarkdown
        remarkPlugins={[remarkMath]}
        // 顺序:先 KaTeX 后代码高亮,互不干扰。rehype-highlight 跟随
        // ```lang 围栏注入 hljs + language-xxx 类(D3-T2);无语言围栏
        // 的代码块不注入语言类,保持原样式。
        rehypePlugins={[rehypeKatex, rehypeHighlight]}
        components={{
          code({ children, className }) {
            // rehype-highlight 注入的 hljs + language-xxx 类必须保留
            // (整体覆盖会导致高亮样式丢失——D3-T2 关键点);无语言
            // 围栏时 className 为空,保持等宽/字号基础样式。
            return (
              <code
                className={[className, "font-mono text-caption"]
                  .filter(Boolean)
                  .join(" ")}
              >
                {children}
              </code>
            );
          },
          pre({ children }) {
            return (
              <pre className="overflow-x-auto rounded-md bg-neutral-900 p-3 text-neutral-50">
                {children}
              </pre>
            );
          },
        }}
        skipHtml
      >
        {content}
      </ReactMarkdown>
    </MarkdownErrorBoundary>
  );
}
