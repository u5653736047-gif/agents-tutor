"use client";

import { Component, memo, useMemo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import {
  repairUnclosedFence,
  splitMarkdownBlocks,
} from "../lib/assistant/markdown-streaming";
import { CodeBlock, textFromChildren } from "./code-block";

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

// T11:插件链与组件映射提升为模块级常量——ReactMarkdown 的 props 引用稳定
// 是块级 memo 生效的前提(内联对象每次渲染都是新引用,memo 会被击穿)。
// remark-gfm:GFM 表格(| a | b |)在 CommonMark 下不解析,D3-T3 表格样式
// 依赖它;顺序:先 KaTeX 后代码高亮,互不干扰。
const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex, rehypeHighlight];

// UX-20260807#3:结构映射——Tailwind v4 preflight 把 h1-h6 字号重置为
// inherit、列表 list-style 清零、blockquote/a 无装饰,不映射则回答正文
// 「一面墙」。字号只用语义档位(text-title/heading/body),无硬编码色值。
// Components 注解提供上下文类型(模块级常量无 JSX 实参的推断上下文)。
const MARKDOWN_COMPONENTS: Components = {
  h1({ children }) {
    return <h1 className="mt-6 mb-3 text-title font-semibold">{children}</h1>;
  },
  h2({ children }) {
    return <h2 className="mt-6 mb-2 text-heading font-semibold">{children}</h2>;
  },
  h3({ children }) {
    return <h3 className="mt-5 mb-1.5 text-body font-semibold">{children}</h3>;
  },
  h4({ children }) {
    return (
      <h4 className="mt-4 mb-1 text-body font-semibold text-muted-foreground">
        {children}
      </h4>
    );
  },
  p({ children }) {
    return <p className="my-2 first:mt-0 last:mb-0">{children}</p>;
  },
  ul({ children }) {
    return <ul className="my-3 list-disc space-y-1.5 pl-5">{children}</ul>;
  },
  ol({ children }) {
    return <ol className="my-3 list-decimal space-y-1.5 pl-5">{children}</ol>;
  },
  blockquote({ children }) {
    return (
      <blockquote className="my-3 border-l-2 border-primary/40 pl-3 text-muted-foreground">
        {children}
      </blockquote>
    );
  },
  a({ children, href }) {
    // 外链新标签打开(回答内链接均为外部资料);品牌色 + 下划线,
    // 可辨识为链接(可访问性)。
    return (
      <a
        className="text-primary underline underline-offset-2 hover:text-primary/80"
        href={href}
        rel="noreferrer"
        target="_blank"
      >
        {children}
      </a>
    );
  },
  strong({ children }) {
    return <strong className="font-semibold">{children}</strong>;
  },
  hr() {
    // 全局 `* { border-color: var(--border) }` 已提供边框色,无需重复 border-border。
    return <hr className="my-4 border-t" />;
  },
  code({ children, className }) {
    // rehype-highlight 注入的 hljs + language-xxx 类必须保留(整体覆盖会
    // 导致高亮样式丢失——D3-T2 关键点);无语言围栏时 className 为空,
    // 保持等宽/字号基础样式。
    return (
      <code
        className={[className, "font-mono text-caption"].filter(Boolean).join(" ")}
      >
        {children}
      </code>
    );
  },
  pre({ children }) {
    // D3-T3:代码块右上角复制按钮。textFromChildren 提取高亮后 code 的
    // 纯文本作为复制内容;pre 的 pt-8 给绝对定位的按钮让位。
    return (
      <CodeBlock text={textFromChildren(children)}>
        <pre className="overflow-x-auto rounded-md bg-neutral-900 p-3 pt-8 text-neutral-50">
          {children}
        </pre>
      </CodeBlock>
    );
  },
  table({ children }) {
    // D3-T3:表格边框/横向滚动。data-slot 供测试与样式锚定。
    return (
      <div className="my-3 overflow-x-auto" data-slot="markdown-table">
        <table className="w-full border-collapse text-body">{children}</table>
      </div>
    );
  },
  thead({ children }) {
    return <thead className="bg-muted text-left">{children}</thead>;
  },
  th({ children }) {
    return <th className="border border-border px-3 py-1.5 font-medium">{children}</th>;
  },
  td({ children }) {
    return <td className="border border-border px-3 py-1.5 align-top">{children}</td>;
  },
};

// T11:单个 Markdown 块的渲染器(块级 memo)——流式追加时只有末尾活跃块的
// content 变化,已完成块字符串值相等直接跳过重解析/重渲染(自定义比较:
// 字符串 === 即值相等,无需关心插件/映射引用——它们已是模块级常量)。
const MarkdownBlock = memo(
  function MarkdownBlock({ content }: { content: string }) {
    return (
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={MARKDOWN_COMPONENTS}
        skipHtml
      >
        {content}
      </ReactMarkdown>
    );
  },
  (previous, next) => previous.content === next.content,
);

export function AssistantMarkdown({ content }: AssistantMarkdownProps) {
  // T11:分块只在 content 变化时重算(流式每 token 一次 O(n) 行扫描,
  // 远低于原先的全量重解析);末尾活跃块做围栏修复(未闭合 ``` 不把
  // 后续内容吞进代码块),已完成块原样渲染。
  const blocks = useMemo(() => splitMarkdownBlocks(content), [content]);
  const lastIndex = blocks.length - 1;
  return (
    <MarkdownErrorBoundary content={content}>
      {/* UX-20260807#3:markdown-body 包裹层——globals.css 的行内 code
          样式(.markdown-body :not(pre) > code)以此为作用域,不泄漏到
          回答气泡之外。 */}
      <div className="markdown-body">
        {blocks.map((block, index) => (
          <MarkdownBlock
            content={index === lastIndex ? repairUnclosedFence(block) : block}
            key={index}
          />
        ))}
      </div>
    </MarkdownErrorBoundary>
  );
}
