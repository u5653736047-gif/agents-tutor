import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

// S5-C2:知识文档结构树展示(评审 🔴 修复补测——验收要求「前端单测覆盖
// 树/扁平两态」)。DocumentTreeView 为纯展示组件(props 驱动、无取数),
// SSR 直渲染锁定两形态的输出结构。

import {
  DocumentTreeView,
  type TreeResponse,
} from "../components/document-tree-view";

function treeResponse(overrides: Partial<TreeResponse> = {}): TreeResponse {
  return {
    kind: "tree",
    document_id: "ml-demo",
    chapters: [
      {
        chapter: "第1章 概述",
        chunk_count: 3,
        sections: [
          { section: "1.1 背景", chunk_count: 1, tags: ["历史"] },
          { section: "1.2 目标", chunk_count: 2, tags: [] },
        ],
      },
      {
        chapter: "第2章 方法",
        chunk_count: 1,
        sections: [],
      },
    ],
    flat_pages: [],
    ...overrides,
  };
}

test("the document tree renders chapters, sections, badges and tags", () => {
  const markup = renderToStaticMarkup(createElement(DocumentTreeView, { tree: treeResponse() }));

  // 树形态容器与两章标题
  assert.match(markup, /data-slot="document-tree"/);
  assert.match(markup, /第1章 概述/);
  assert.match(markup, /第2章 方法/);
  // 章/节 chunk 计数徽标
  assert.match(markup, /data-slot="document-tree-chunk-badge"[^>]*>3</);
  assert.match(markup, /data-slot="document-tree-section-badge"[^>]*>1</);
  assert.match(markup, /data-slot="document-tree-section-badge"[^>]*>2</);
  // 小节标题与 tags 汇总
  assert.match(markup, /data-slot="document-tree-section"/);
  assert.match(markup, /1\.1 背景/);
  assert.match(markup, /历史/);
});

test("the document tree falls back to a flat page list without structure", () => {
  const markup = renderToStaticMarkup(
    createElement(DocumentTreeView, {
      tree: treeResponse({ kind: "flat", chapters: [], flat_pages: [3, 1, 2] }),
    }),
  );

  assert.match(markup, /data-slot="document-tree-flat"/);
  assert.match(markup, /无章节结构，按页平铺：/);
  const pages = markup.match(/data-slot="document-tree-page"/g) ?? [];
  assert.equal(pages.length, 3);
  assert.match(markup, /第 3 页/);
});

test("the document tree shows an empty placeholder when flat has no pages", () => {
  const markup = renderToStaticMarkup(
    createElement(DocumentTreeView, {
      tree: treeResponse({ kind: "flat", chapters: [], flat_pages: [] }),
    }),
  );

  assert.match(markup, /该文档无可展示内容/);
  assert.doesNotMatch(markup, /data-slot="document-tree-page"/);
});
