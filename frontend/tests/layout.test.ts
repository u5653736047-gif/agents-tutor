import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

// D4-T6:根布局无法直接 SSR 渲染(RootLayout 依赖 next 环境),与既有
// app-shell.test.ts 的源码正则先例一致,用 readFileSync 守卫。
const layoutPath = new URL("../app/layout.tsx", import.meta.url);
const globalsPath = new URL("../app/globals.css", import.meta.url);

test("the root layout wraps children in the next-themes ThemeProvider", () => {
  assert.ok(existsSync(layoutPath), "missing root layout");
  const source = readFileSync(layoutPath, "utf8");

  // ThemeProvider 来自 next-themes;attribute="class" 使暗色类落在 <html>
  assert.match(source, /import \{[^}]*ThemeProvider[^}]*\} from "next-themes"/);
  // suppressHydrationWarning 避免 next-themes 内联脚本改 <html> 类时与
  // React hydration 告警冲突;defaultTheme="system" 让首屏跟随系统偏好
  assert.match(source, /<html lang="zh-CN" suppressHydrationWarning>/);
  assert.match(source, /<ThemeProvider attribute="class" defaultTheme="system" enableSystem>/);
  // children 被 ThemeProvider 包裹
  assert.match(source, /<ThemeProvider[\s\S]*?>\s*\{children\}\s*<\/ThemeProvider>/);
});

test("globals.css registers the class-based dark variant", () => {
  // D4-T6 review blocking 修复:Tailwind v4 的 dark: 变体默认是
  // prefers-color-scheme 媒体查询,不认 html.dark 类——必须显式
  // @custom-variant dark 注册 class 策略,否则手动切换主题后图标与
  // 页面状态错位(系统亮色切暗色 → 页面变暗但月亮图标仍显示)。
  const css = readFileSync(globalsPath, "utf8");
  assert.match(css, /@custom-variant dark \(&:where\(\.dark, \.dark \*\)\)/);
  // .dark 语义 token 覆盖存在(review 同时固化此断言)
  assert.match(css, /\.dark \{[\s\S]*?--background:/);
});
