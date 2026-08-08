// D5-T3:Skeleton 骨架屏组件的 SSR 测试——shadcn 风格占位类
// (animate-pulse 呼吸 + 语义 token bg-muted 自动适配亮/暗)与自定义
// className 追加;组件无 props 依赖,SSR 直渲即可覆盖。
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

test("Skeleton renders the shadcn placeholder classes", async () => {
  const { Skeleton } = await import("../components/ui/skeleton");

  // 默认:animate-pulse 呼吸 + rounded-md + bg-muted(语义 token)
  const markup = renderToStaticMarkup(createElement(Skeleton));
  assert.match(markup, /animate-pulse rounded-md bg-muted/);

  // 自定义 className 追加(尺寸/形状由调用方拼装;cn 的 tailwind-merge
  // 负责类去重,这里用与默认类不冲突的 size-8 验证追加)
  const custom = renderToStaticMarkup(
    createElement(Skeleton, { className: "size-8" }),
  );
  assert.match(custom, /animate-pulse rounded-md bg-muted size-8/);
});
