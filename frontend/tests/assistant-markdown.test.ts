import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const markdownPath = new URL("../components/assistant-markdown.tsx", import.meta.url);

async function loadAssistantMarkdown() {
  assert.ok(existsSync(markdownPath), "missing safe assistant Markdown renderer");
  return import("../components/assistant-markdown");
}

test("assistant Markdown renders formatting and code blocks without raw HTML", async () => {
  const { AssistantMarkdown } = await loadAssistantMarkdown();
  const markup = renderToStaticMarkup(
    createElement(AssistantMarkdown, {
      content: "**重点**\n\n```ts\nconst value = 1;\n```\n\n<script>alert('unsafe')</script>",
    }),
  );

  assert.match(markup, /<strong>重点<\/strong>/);
  assert.match(markup, /font-mono/);
  assert.match(markup, /bg-neutral-900/);
  assert.doesNotMatch(markup, /<script/i);
  assert.doesNotMatch(markup, /unsafe/);
});

test("the Markdown error boundary falls back to the raw assistant text", async () => {
  const { MarkdownErrorBoundary } = await loadAssistantMarkdown();
  const boundary = new MarkdownErrorBoundary({ children: null, content: "保留原文" });

  boundary.state = MarkdownErrorBoundary.getDerivedStateFromError();
  const markup = renderToStaticMarkup(boundary.render());

  assert.match(markup, /data-slot="markdown-fallback"/);
  assert.match(markup, /保留原文/);
});

test("assistant Markdown renders inline math with KaTeX", async () => {
  const { AssistantMarkdown } = await loadAssistantMarkdown();
  const markup = renderToStaticMarkup(
    createElement(AssistantMarkdown, {
      content: "向量内积 $a \\cdot b$ 定义。",
    }),
  );

  // KaTeX 渲染产物的特征:外层 span 带 katex 类
  assert.match(markup, /class="katex"/);
  // markdown 的 $...$ 语法已被公式替换,美元符不再直接出现
  // (注意:KaTeX 的 mathml annotation 会保留 TeX 源码,因此不断言 \cdot 消失)
  assert.doesNotMatch(markup, /\$/);
});

test("assistant Markdown renders block math with katex-display", async () => {
  const { AssistantMarkdown } = await loadAssistantMarkdown();
  const markup = renderToStaticMarkup(
    createElement(AssistantMarkdown, {
      // 块级公式必须 $$ 独立成行(micromark-extension-math 的 flow
      // 语法);单行 "$$E = mc^2$$" 会被解析为行内公式(review 复测
      // 确认,测试锁定块级真实形态)。
      content: "$$\nE = mc^2\n$$",
    }),
  );

  // 块级公式特征:katex-display 外层 + display="block" 的 MathML
  assert.match(markup, /katex-display/);
  assert.match(markup, /display="block"/);
  assert.doesNotMatch(markup, /\$\$/);
});

test("assistant Markdown treats an unclosed dollar sign as plain text", async () => {
  const { AssistantMarkdown } = await loadAssistantMarkdown();
  const markup = renderToStaticMarkup(
    createElement(AssistantMarkdown, { content: "未闭合公式 $a \\cdot b" }),
  );

  // remark-math 对未闭合的 $ 不生成公式节点,原样输出文本,不炸页
  assert.match(markup, /未闭合公式 \$a \\cdot b/);
  assert.doesNotMatch(markup, /katex/);
  assert.doesNotMatch(markup, /markdown-fallback/);
});

test("assistant Markdown tolerates invalid TeX without crashing", async () => {
  const { AssistantMarkdown } = await loadAssistantMarkdown();
  // \q 不是 KaTeX 命令;throwOnError:false 下 KaTeX 不抛异常,把未知
  // 命令渲染为红色错误文本(mathcolor="#cc0000" 的 mstyle/mtext,
  // 实测确认——不是 katex-error span,后者只用于括号不匹配等 parse
  // 错误),页面不炸、不触发边界兜底。
  const markup = renderToStaticMarkup(
    createElement(AssistantMarkdown, { content: "非法公式 $a \\q$" }),
  );

  assert.match(markup, /mathcolor="#cc0000"/);
  assert.match(markup, /class="katex"/);
  assert.doesNotMatch(markup, /data-slot="markdown-fallback"/);
});
