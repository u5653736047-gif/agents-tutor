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
  // UX-20260807#2:isLoadingSessions 初始 true——SSR 首帧渲染侧栏骨架
  // 而非「暂无会话」空态(修首帧闪空态)
  assert.match(markup, /data-slot="session-skeleton"/);
  assert.doesNotMatch(markup, /暂无会话/);
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
  // D5-T5:遮罩/选中会话/Esc 统一走 closeDrawer(关闭 + 焦点归还单点)
  assert.match(source, /onClick=\{closeDrawer\}/);
  // Esc 关闭:仅抽屉打开时注册 keydown 监听
  assert.match(source, /addEventListener\("keydown"/);
  assert.match(source, /event\.key === "Escape"/);
  // D5-T5:Esc 处理器调用 closeDrawer 而非直接 setSidebarOpen(false)
  assert.match(source, /event\.key === "Escape"\) \{\s*\n\s*closeDrawer\(\);/);
  // 方案 A:移动抽屉分支给 SessionSidebar 传 onSessionSelected 回调
  // (选中会话即收起;D5-T5 走 closeDrawer 归还焦点)
  assert.match(source, /onSessionSelected=\{closeDrawer\}/);
});

// D5-T5:抽屉焦点管理源码正则守卫——打开时焦点移入抽屉容器(tabIndex=-1),
// 关闭(遮罩/Esc/选中会话)统一经 closeDrawer 归还汉堡按钮。动态焦点行为
// 无法在 SSR/无 jsdom 环境运行,实现要点由源码正则锁定,完整流程走手动验收。
test("the mobile drawer moves focus in on open and returns it on close", () => {
  const source = readFileSync(appShellPath, "utf8");

  // 抽屉容器 tabIndex={-1} + ref:可被程序化聚焦
  assert.match(source, /data-slot="sidebar-drawer"/);
  assert.match(source, /tabIndex=\{-1\}/);
  assert.match(source, /ref=\{drawerRef\}/);
  // 打开时焦点移入:effect 内只做 DOM 焦点同步(focus(),不 setState——
  // react-hooks lint 认可「与外部系统同步」的合法用法)
  assert.match(
    source,
    /if \(sidebarOpen\) \{\s*\n\s*drawerRef\.current\?\.focus\(\)/,
  );
  // 关闭统一入口:setState + 焦点归还汉堡按钮(useCallback 空依赖稳定)
  assert.match(
    source,
    /const closeDrawer = useCallback\(\(\) => \{\s*\n\s*setSidebarOpen\(false\);\s*\n\s*toggleRef\.current\?\.focus\(\)/,
  );
  // 汉堡按钮持有 ref(归还目标)
  assert.match(source, /data-slot="sidebar-toggle"[\s\S]*?ref=\{toggleRef\}/);
});

// D4-T6:主题切换的 SSR 断言。useTheme 在无 ThemeProvider 时返回默认值
// (next-themes 不抛错),SSR 初始态 mounted=false → 月亮图标 +
// 「切换到暗色模式」;点击互切是客户端交互,由源码正则守卫。
test("the app shell renders the theme toggle in its initial state", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  assert.match(markup, /data-slot="theme-toggle"/);
  assert.match(markup, /切换到暗色模式/);
});

// D4-T6:主题切换逻辑的源码正则守卫(CSS 类驱动图标 + setTheme)。
test("the theme toggle switches between light and dark themes", () => {
  const source = readFileSync(appShellPath, "utf8");

  // 图标 CSS 类驱动:亮色显月亮(dark:hidden)、暗色显太阳(dark:block)
  // ——next-themes 内联脚本在 hydration 前设置 html 的 dark 类,CSS
  // 即时生效,无 JS 状态、无闪烁(react-hooks 新 lint 拦截 effect 内
  // setState,mounted 模式被弃用,注释见组件)。
  assert.match(source, /<Moon aria-hidden className="size-5 dark:hidden" \/>/);
  assert.match(source, /<Sun aria-hidden className="hidden size-5 dark:block" \/>/);
  // aria-label 与 onClick 读 resolvedTheme,点击在亮/暗之间互切
  assert.match(source, /setTheme\(resolvedTheme === "dark" \? "light" : "dark"\)/);
  assert.match(source, /切换到亮色模式/);
  assert.match(source, /切换到暗色模式/);
});

// D5-T2:抽屉/遮罩进入动画的源码正则守卫。SSR 初始态不渲染抽屉,
// 动画类写在 JSX 里只在打开挂载时生效(无 mismatch);关闭走即时
// 卸载不做退出动画(注释见组件)。
test("the mobile drawer and overlay carry enter animations", () => {
  const source = readFileSync(appShellPath, "utf8");

  // 遮罩:淡入(200ms,对齐 D5-T1 tokens)
  assert.match(
    source,
    /className="[^"]*animate-in fade-in-0[^"]*"[^>]*data-slot="sidebar-overlay"/,
  );
  assert.match(source, /duration-\[var\(--app-duration-normal\)\]/);
  // 抽屉:淡入 + 左侧滑入(tw-animate-css slide-in-from-left-2)
  assert.match(source, /animate-in fade-in-0 slide-in-from-left-2/);
});

// D5-T4:空态引导与示例问题卡的 SSR 断言。useSyncExternalStore 服务端
// 快照恒 false(getServerSnapshot = () => false),SSR 首帧必渲染示例卡与
// 引导;「已看过」标记只在客户端 hydration 后生效(React 官方「服务端
// 默认值 + 客户端真实值」模式)。
test("the empty state renders example questions and first-use onboarding", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  // 示例问题卡 + 4 个示例问题按钮
  assert.match(markup, /data-slot="example-questions"/);
  assert.equal((markup.match(/data-slot="example-question"/g) ?? []).length, 4);
  // 首次使用引导 + 跳过按钮
  assert.match(markup, /data-slot="onboarding"/);
  assert.match(markup, /data-slot="onboarding-skip"/);
  assert.match(markup, /跳过引导/);
  // 既有空态标题保留(零回归)
  assert.match(markup, /请选择或新建会话/);
});

// D5-T4:示例问题点击时序的源码正则守卫——建会话成功后流式发送问题,
// 失败(createSession 返回 null)不发送;跳过按钮直接调用 markOnboardingSeen。
test("example question clicks create a session then stream the question", () => {
  const source = readFileSync(appShellPath, "utf8");

  assert.match(source, /const session = await createSession\(\)/);
  assert.match(source, /if \(session\)/);
  assert.match(source, /streamSendMessage\(question\)/);
  // 跳过引导:写入本地标记(事件驱动订阅者重渲染隐藏引导)
  assert.match(source, /data-slot="onboarding-skip"[^>]*onClick=\{markOnboardingSeen\}/);
});

// D6-T4:顶栏知识库入口(教师端)——SSR 渲染 + 源码守卫。链接本身是
// 纯静态导航(next/link),SSR 直渲可断言;零回归检查顶栏既有元素。
test("the app shell header links to the knowledge search page", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  assert.match(markup, /data-slot="knowledge-link"/);
  assert.match(markup, /href="\/knowledge"/);
  assert.match(markup, />知识库</);
  // 零回归:既有顶栏元素(连接徽章/主题按钮)仍在
  assert.match(markup, /后端已连接/);
  assert.match(markup, /data-slot="theme-toggle"/);
});

test("the app shell knowledge link uses next/link with muted caption styling", () => {
  const source = readFileSync(appShellPath, "utf8");

  assert.match(source, /import Link from "next\/link"/);
  assert.match(source, /data-slot="knowledge-link"[\s\S]*?href="\/knowledge"/);
  // UX-20260807#4:顶栏入口 hover 品牌化(hover:text-primary)
  assert.match(source, /text-caption text-muted-foreground hover:text-primary/);
});

// D6-T7:顶栏学习进度入口——SSR 渲染 + 源码守卫,与 knowledge-link 并列。
test("the app shell header links to the stats page", async () => {
  const { AppShell } = await loadAppShell();
  const markup = renderToStaticMarkup(createElement(AppShell, { apiConnected: true }));

  assert.match(markup, /data-slot="stats-link"/);
  assert.match(markup, /href="\/stats"/);
  assert.match(markup, />进度</);
  // 零回归:既有顶栏链接与元素仍在
  assert.match(markup, /data-slot="knowledge-link"/);
  assert.match(markup, /后端已连接/);
  assert.match(markup, /data-slot="theme-toggle"/);
});

test("the app shell stats link uses next/link with muted caption styling", () => {
  const source = readFileSync(appShellPath, "utf8");

  assert.match(source, /data-slot="stats-link"[\s\S]*?href="\/stats"/);
  // UX-20260807#4:顶栏入口 hover 品牌化(hover:text-primary)
  assert.match(source, /text-caption text-muted-foreground hover:text-primary/);
});
