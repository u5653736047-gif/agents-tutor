// D4-T1:会话侧栏搜索的测试。
// 过滤与高亮逻辑抽为纯函数(filterSessions / highlightMatch)直接断言;
// 组件 SSR 只验证搜索框存在与空查询(SSR 初始 state)渲染全部会话。
// 输入防抖(200ms)与过滤交互是客户端行为,SSR 无法模拟,留给手动验收
// ——与 collaboration-panel.test.ts 对折叠交互的处理先例一致。
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const sidebarPath = new URL("../components/session-sidebar.tsx", import.meta.url);

async function loadSessionSidebar() {
  assert.ok(existsSync(sidebarPath), "missing session sidebar");
  return import("../components/session-sidebar");
}

// 与契约 Session 形状一致的最小样本(大小写混用,覆盖大小写不敏感场景)
const sessions = [
  { archived: false, created_at: "2025-01-01T00:00:00Z", session_id: "session-abc-1", user_id: null },
  { archived: false, created_at: "2025-01-02T00:00:00Z", session_id: "Session-XYZ-2", user_id: "u1" },
  { archived: true, created_at: "2025-01-03T00:00:00Z", session_id: "demo-000", user_id: null },
];

test("filterSessions matches session_id case-insensitively", async () => {
  const { filterSessions } = await loadSessionSidebar();

  // 小写查询命中小写 ID
  assert.deepEqual(
    filterSessions(sessions, "abc").map((session) => session.session_id),
    ["session-abc-1"],
  );
  // 大小写不敏感:查询词大写、ID 大小写混写也能命中
  assert.deepEqual(
    filterSessions(sessions, "SESSION-XYZ").map((session) => session.session_id),
    ["Session-XYZ-2"],
  );
});

test("filterSessions returns all sessions for an empty query", async () => {
  const { filterSessions } = await loadSessionSidebar();

  // 空查询返回全部(原数组引用);纯空白视为无查询
  assert.equal(filterSessions(sessions, ""), sessions);
  assert.equal(filterSessions(sessions, "   "), sessions);
  // 未命中返回空数组
  assert.deepEqual(filterSessions(sessions, "not-found"), []);
});

test("highlightMatch splits session id into prefix, hit, and suffix", async () => {
  const { highlightMatch } = await loadSessionSidebar();

  // 命中切三段:前缀 / 命中(highlighted) / 后缀
  assert.deepEqual(highlightMatch("session-abc-1", "abc"), [
    { highlighted: false, text: "session-" },
    { highlighted: true, text: "abc" },
    { highlighted: false, text: "-1" },
  ]);
  // 大小写不敏感:高亮保留原始大小写(命中段为 "XYZ" 而非 "xyz")
  assert.deepEqual(highlightMatch("Session-XYZ-2", "xyz"), [
    { highlighted: false, text: "Session-" },
    { highlighted: true, text: "XYZ" },
    { highlighted: false, text: "-2" },
  ]);
});

test("highlightMatch returns a single non-highlighted segment for empty or unmatched query", async () => {
  const { highlightMatch } = await loadSessionSidebar();

  assert.deepEqual(highlightMatch("session-abc-1", ""), [
    { highlighted: false, text: "session-abc-1" },
  ]);
  assert.deepEqual(highlightMatch("session-abc-1", "zzz"), [
    { highlighted: false, text: "session-abc-1" },
  ]);
});

// D4-T1:组件 SSR——搜索框存在;空查询(SSR 初始 state)渲染全部会话、
// 不出现「未找到」占位。过滤态由上方纯函数测试覆盖。
test("the session sidebar renders a search box and all sessions on initial render", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  const baseProps = {
    archiveSession: () => undefined,
    createSession: () => undefined,
    currentSessionId: null,
    isLoadingSessions: false,
    loadCurrentSessionMessages: () => undefined,
    requestError: null,
    selectSession: () => undefined,
  };
  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, { ...baseProps, sessions }),
  );

  assert.match(markup, /data-slot="session-sidebar"/);
  assert.match(markup, /data-slot="session-search"/);
  // 空查询:全部会话可见,无搜索空态占位
  assert.match(markup, /session-abc-1/);
  assert.match(markup, /Session-XYZ-2/);
  assert.match(markup, /demo-000/);
  assert.doesNotMatch(markup, /data-slot="session-search-empty"/);
  // 无会话时保留「暂无会话」空态
  const emptyMarkup = renderToStaticMarkup(
    createElement(SessionSidebarContent, { ...baseProps, sessions: [] }),
  );
  assert.match(emptyMarkup, /暂无会话/);
});

test("SessionSidebar container keeps the mount-time session refresh", async () => {
  // review 修正的回归守卫:重构曾误删容器挂载时的 refreshSessions
  // 调用,导致应用启动后侧栏永远「暂无会话」。用源码正则锁定(与
  // app-shell.test.ts 的先例一致;SSR 无法触发 effect)。
  const source = readFileSync(
    new URL("../components/session-sidebar.tsx", import.meta.url),
    "utf-8",
  );
  assert.match(source, /useEffect\(\(\) => \{[\s\S]*?void refreshSessions\(\)/);
});
