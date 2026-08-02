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
