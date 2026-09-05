import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

// S5-C1 决策 5/6:知识空间选择器(评审 🔴 修复补测)。覆盖:
// - 「＋ 新建空间」入口存在(自举问题解法——第一个课程空间无从选择时
//   需要手动创建入口,不能只依赖聚合端点);
// - 新建空间校验规则与后端正则语义一致(小写标识 + public 保留值拒绝);
// - SSR 初始渲染含 public 默认项(effects 不在 SSR 执行,取数失败静默降级)。

import {
  NamespaceSelector,
  isValidNewNamespace,
} from "../components/namespace-selector";

test("the namespace selector renders public default and a create entry", () => {
  const markup = renderToStaticMarkup(
    createElement(NamespaceSelector, { value: "public", onChange: () => {} }),
  );

  assert.match(markup, /data-slot="namespace-selector"/);
  assert.match(markup, /data-slot="namespace-select"/);
  // public 为保留公共库空间,默认可选且带说明文案
  assert.match(markup, /public（公共教材库）/);
  // 自举问题解法:聚合端点之外必须有「＋ 新建空间」手动入口
  assert.match(markup, /data-slot="namespace-create-toggle"/);
});

test("isValidNewNamespace mirrors the backend lowercase identifier rule", () => {
  // 合法:小写字母开头,只含小写字母/数字/单层内连字符(与后端
  // [a-z][a-z0-9]*(?:-[a-z0-9]+)* 全匹配语义一致)
  assert.equal(isValidNewNamespace("ml-course"), true);
  assert.equal(isValidNewNamespace("ml-course-2026"), true);
  assert.equal(isValidNewNamespace("a1"), true);

  // 非法:大写/数字开头/空串/首尾或连续连字符
  assert.equal(isValidNewNamespace("ML-Course"), false);
  assert.equal(isValidNewNamespace("1course"), false);
  assert.equal(isValidNewNamespace(""), false);
  assert.equal(isValidNewNamespace("-course"), false);
  assert.equal(isValidNewNamespace("course-"), false);
  assert.equal(isValidNewNamespace("ml--course"), false);

  // 保留值:public 不允许被新建空间占用
  assert.equal(isValidNewNamespace("public"), false);
});
