import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const appShellPath = new URL("../components/app-shell.tsx", import.meta.url);

async function loadAppShell() {
  assert.ok(existsSync(appShellPath), "missing two-column application shell");
  return import("../components/app-shell");
}

test("the application shell renders a desktop session sidebar and empty conversation area", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  assert.match(markup, /data-layout="desktop-two-column"/);
  assert.match(markup, /data-slot="session-sidebar"/);
  assert.match(markup, /新建会话/);
  assert.match(markup, /暂无会话/);
  assert.match(markup, /data-slot="conversation-area"/);
  assert.match(markup, /后端已连接/);
});

test("selecting a session asks the store to load its history", () => {
  const sidebarPath = new URL("../components/session-sidebar.tsx", import.meta.url);

  assert.ok(existsSync(sidebarPath), "missing session sidebar");
  const sidebar = readFileSync(sidebarPath, "utf8");
  assert.match(sidebar, /loadCurrentSessionMessages/);
  assert.match(sidebar, /selectSession\(session\.session_id\);\s*void loadCurrentSessionMessages\(\)/);
});

// D4-T5:移动端抽屉的 SSR 断言。开合(点击汉堡/遮罩、Esc)是客户端
// 交互,SSR 无法模拟,留给手动验收覆盖;SSR 只锁定初始态与断点语义。
test("the mobile layout shows only the toggle and no drawer on initial render", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  // 汉堡按钮存在且仅移动端可见(md:hidden)
  assert.match(markup, /md:hidden[^>]*data-slot="sidebar-toggle"/);
  // SSR 初始态抽屉关闭:无遮罩;侧栏仅桌面分支渲染一次
  assert.doesNotMatch(markup, /data-slot="sidebar-overlay"/);
  assert.equal((markup.match(/data-slot="session-sidebar"/g) ?? []).length, 1);
});

// D4-T5:抽屉开合逻辑的源码正则守卫(SSR 无法触发 effect/点击,
// 与既有 app-shell/session-sidebar 测试的先例一致)。
test("the app shell closes the mobile drawer on overlay, Escape, and session selection", () => {
  const source = readFileSync(appShellPath, "utf8");

  // 断点语义:移动端单栏 + md 起桌面两栏
  // 断点类可能被中间类(bg-background 等)隔开,用 [\s\S]*? 放宽
  assert.match(source, /grid-cols-1[\s\S]*?md:grid-cols-\[18rem_minmax\(0,1fr\)\]/);
  // 汉堡按钮打开抽屉
  assert.match(source, /data-slot="sidebar-toggle"/);
  assert.match(source, /setSidebarOpen\(true\)/);
  // 遮罩点击收起
  assert.match(source, /data-slot="sidebar-overlay"/);
  assert.match(source, /onClick=\{\(\) => setSidebarOpen\(false\)\}/);
  // Esc 关闭:仅抽屉打开时注册 keydown 监听
  assert.match(source, /addEventListener\("keydown"/);
  assert.match(source, /event\.key === "Escape"/);
  // 方案 A:移动抽屉分支给 SessionSidebar 传 onSessionSelected 回调
  // (选中会话即收起);桌面分支不传(向后兼容)
  assert.match(source, /onSessionSelected=\{\(\) => setSidebarOpen\(false\)\}/);
});
