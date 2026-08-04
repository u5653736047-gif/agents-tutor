import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const componentPath = new URL("../components/citation-list.tsx", import.meta.url);

async function loadCitationList() {
  assert.ok(existsSync(componentPath), "missing citation list component");
  return import("../components/citation-list");
}

// D3-T4:引用卡片列表渲染测试。
// 两条引用的字段与后端契约一致(document_id / source / page / chunk_id)。
const citations = [
  {
    chunk_id: "ml-zhouzhihua:88:0:500",
    document_id: "ml-zhouzhihua",
    page: 88,
    source: "ml-zhouzhihua",
  },
  {
    chunk_id: "ml-zhouzhihua:120:0:400",
    document_id: "ml-zhouzhihua",
    page: 120,
    source: "ml-zhouzhihua",
  },
];

test("the citation list renders numbered items with source labels and view buttons", async () => {
  const { CitationList } = await loadCitationList();

  assert.equal(typeof CitationList, "function", "missing citation list renderer");
  const markup = renderToStaticMarkup(
    createElement(CitationList, { citations }),
  );

  // 编号列表容器 + 每条引用一个条目 + 来源文本
  assert.match(markup, /data-slot="citation-list"/);
  assert.match(markup, /data-slot="citation-item"/);
  assert.equal(markup.match(/data-slot="citation-item"/g)?.length, 2);
  assert.match(markup, /\[1\]/);
  assert.match(markup, /\[2\]/);
  assert.match(markup, /ml-zhouzhihua/);
  // 每条都有「查看」按钮(展开交互依赖组件内 useState,SSR 只断言按钮存在;
  // 点击展开/收起的交互由手动验收覆盖)
  assert.equal(markup.match(/data-slot="citation-toggle"/g)?.length, 2);
  assert.match(markup, /查看/);
});

test("the citation list starts collapsed with no detail rendered", async () => {
  const { CitationList } = await loadCitationList();

  const markup = renderToStaticMarkup(
    createElement(CitationList, { citations }),
  );

  // 初始展开状态为 null:详情区零渲染,「查看」按钮 aria-expanded=false
  assert.doesNotMatch(markup, /data-slot="citation-detail"/);
  assert.match(markup, /aria-expanded="false"/);
});

test("the citation list renders nothing for null citations", async () => {
  const { CitationList } = await loadCitationList();

  // 降级红线:字段缺失(null)时零渲染——renderToStaticMarkup 对
  // return null 的组件输出空字符串,不得有占位或报错文本
  const markup = renderToStaticMarkup(createElement(CitationList, { citations: null }));

  assert.equal(markup, "");
});

test("the citation list renders nothing for an empty citations array", async () => {
  const { CitationList } = await loadCitationList();

  // 降级红线:空数组与 null 同等对待,零渲染(不渲染「无引用」占位)
  const markup = renderToStaticMarkup(createElement(CitationList, { citations: [] }));

  assert.equal(markup, "");
});
