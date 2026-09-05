import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

// AI 生成内容标识(伦理合规):两种形态的渲染契约——
// message(助手消息气泡内常驻)与 footer(页面级全局声明)。
// 纯展示组件,renderToStaticMarkup 直接断言(与 grading-card 测试同构)。

async function loadNotice() {
  return import("../components/ai-content-notice");
}

test("the message variant renders the compact AI content notice", async () => {
  const { AiContentNotice } = await loadNotice();

  const markup = renderToStaticMarkup(createElement(AiContentNotice));

  assert.match(markup, /data-slot="ai-content-notice"/);
  assert.match(markup, /内容由 AI 生成/);
  assert.match(markup, /重要信息请人工复核/);
});

test("the footer variant renders the page-level declaration", async () => {
  const { AiContentNotice } = await loadNotice();

  const markup = renderToStaticMarkup(
    createElement(AiContentNotice, { variant: "footer" }),
  );

  assert.match(markup, /data-slot="ai-content-notice"/);
  assert.match(markup, /本系统内容由人工智能生成或聚合/);
});
