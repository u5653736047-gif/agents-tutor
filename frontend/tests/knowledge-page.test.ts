import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

// D6-T4:知识库检索测试面板(教师端)。
// 页面是 "use client" 客户端组件,提交/结果渲染依赖 fetch,SSR 无法
// 模拟交互——SSR 只锁定初始表单渲染与结果区空态;提交时序、错误
// 归一与 top_k 默认值由源码正则守卫(与 app-shell/feedback 先例一致)。
const knowledgePagePath = new URL("../app/knowledge/page.tsx", import.meta.url);

async function loadKnowledgePage() {
  assert.ok(existsSync(knowledgePagePath), "missing knowledge search page");
  return import("../app/knowledge/page");
}

test("the knowledge page SSR-renders the search form and no result area initially", async () => {
  const { default: KnowledgePage } = await loadKnowledgePage();

  assert.equal(typeof KnowledgePage, "function", "missing default export page component");
  const markup = renderToStaticMarkup(createElement(KnowledgePage));

  // 标题(教师端)+ 返回首页链接(渲染为 <a href="/">)
  assert.match(markup, /知识库检索测试/);
  assert.match(markup, /教师端/);
  assert.match(markup, /href="\/"[^>]*>返回首页</);
  // 表单三件套:query 输入(必填)、top_k 选择、检索按钮
  assert.match(markup, /data-slot="knowledge-query"/);
  assert.match(markup, /data-slot="knowledge-topk"/);
  assert.match(markup, /data-slot="knowledge-search-btn"/);
  assert.match(markup, />检索</);
  // SSR 初始态:未提交 → 结果区四态(加载/错误/空/列表)均不渲染
  assert.doesNotMatch(markup, /data-slot="knowledge-loading"/);
  assert.doesNotMatch(markup, /data-slot="knowledge-error"/);
  assert.doesNotMatch(markup, /data-slot="knowledge-empty"/);
  assert.doesNotMatch(markup, /data-slot="knowledge-hit"/);
});

// 交互实现要点源码守卫:调用时序、top_k 默认、错误归一、结果区四态。
test("the knowledge page calls searchKnowledge with default top_k 5 and normalizes errors", () => {
  const source = readFileSync(knowledgePagePath, "utf8");

  // 客户端组件 + 直接调 api-client(不接 store,避免污染主会话状态)
  assert.match(source, /"use client"/);
  assert.match(source, /apiClient\.searchKnowledge\(\{ query: trimmed, topK \}\)/);
  assert.match(source, /import Link from "next\/link"/);
  assert.match(source, /href="\/"/);
  // top_k 默认 5:调用侧 topK 未传时由 api-client 落 top_k: 5(实现
  // 在 api-client.ts,页面只透传 topK;行为由 api-client.test.ts 覆盖)
  const apiClientSource = readFileSync(
    new URL("../lib/api-client.ts", import.meta.url),
    "utf8",
  );
  assert.match(apiClientSource, /top_k: input\.topK \?\? 5/);
  // 提交中禁用按钮 + 「检索中…」文案
  assert.match(source, /disabled=\{loading\}/);
  assert.match(source, /检索中…/);
  // 错误归一:ApiClientError 分支 + 503 knowledge_unavailable 特殊提示
  assert.match(source, /instanceof ApiClientError/);
  assert.match(source, /error\.code === "knowledge_unavailable"/);
  assert.match(source, /知识库暂不可用/);
  // 错误时清空旧结果,避免残留误导
  assert.match(source, /setError\(errorText\(caught\)\)/);
  assert.match(source, /setResult\(null\)/);
  // 结果区四态:加载(骨架)/错误/空结果/命中列表 + 3 位小数分数
  assert.match(source, /data-slot="knowledge-loading"/);
  assert.match(source, /data-slot="knowledge-error"/);
  assert.match(source, /未找到相关内容/);
  assert.match(source, /data-slot="knowledge-hit"/);
  assert.match(source, /toFixed\(3\)/);
  // 引用行展示 document_id/source/page/chunk_id
  assert.match(source, /data-slot="knowledge-citation"/);
  assert.match(source, /hit\.citation\.document_id/);
  assert.match(source, /hit\.citation\.source/);
  assert.match(source, /hit\.citation\.page \?\? "—"/);
  assert.match(source, /hit\.citation\.chunk_id/);
});
