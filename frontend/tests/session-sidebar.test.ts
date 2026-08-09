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
    onToggleArchived: () => undefined,
    requestError: null,
    selectSession: () => undefined,
    showArchived: false,
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

test("SessionSidebar container forwards an optional onSessionSelected callback", async () => {
  // D4-T5:移动端抽屉「选中会话后自动收起」由容器可选回调实现(方案
  // A);桌面分支不传,向后兼容。源码正则守卫:回调包装 selectSession
  // 并透传(SSR 无法触发交互)。
  const source = readFileSync(
    new URL("../components/session-sidebar.tsx", import.meta.url),
    "utf-8",
  );
  assert.match(source, /onSessionSelected\?: \(\) => void/);
  assert.match(source, /onSessionSelected\?\.\(\)/);
});

// D4-T7:归档视图 ————————————————————————————————
// 归档切换按钮与空态均为展示层逻辑(依赖 showArchived prop),SSR 可测;
// 切换触发重新拉取的行为在 chat-store.test.ts 覆盖。
test("the sidebar renders the archive toggle and the archived empty state", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  const baseProps = {
    archiveSession: () => undefined,
    createSession: () => undefined,
    currentSessionId: null,
    isLoadingSessions: false,
    loadCurrentSessionMessages: () => undefined,
    onToggleArchived: () => undefined,
    requestError: null,
    selectSession: () => undefined,
    showArchived: false,
  };

  // 未归档视图:切换按钮存在,文案为「查看归档」;无会话时为空态「暂无会话」
  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, { ...baseProps, sessions: [] }),
  );
  assert.match(markup, /data-slot="archive-toggle"/);
  assert.match(markup, /查看归档/);
  assert.doesNotMatch(markup, /查看未归档/);
  assert.match(markup, /暂无会话/);
  assert.doesNotMatch(markup, /data-slot="archive-empty"/);

  // 归档视图:按钮文案反转为「查看未归档」;无会话时显示归档空态
  const archivedMarkup = renderToStaticMarkup(
    createElement(SessionSidebarContent, { ...baseProps, showArchived: true, sessions: [] }),
  );
  assert.match(archivedMarkup, /data-slot="archive-empty"/);
  assert.match(archivedMarkup, /暂无归档会话/);
  assert.match(archivedMarkup, /查看未归档/);
  assert.doesNotMatch(archivedMarkup, /暂无会话/);
});

test("the sidebar groups sessions under time-based section titles", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  // 相对当前时间构造三个组的样本(UTC 方法,远离分组边界):
  // 今天 = 当前 UTC 日零点;近 7 天 = 3 天前同一时刻;更早 = 30 天前。
  const now = new Date();
  const today = new Date(now);
  today.setUTCHours(0, 0, 0, 0);
  const recent = new Date(now);
  recent.setUTCDate(recent.getUTCDate() - 3);
  const older = new Date(now);
  older.setUTCDate(older.getUTCDate() - 30);

  const groupedSessions = [
    { archived: false, created_at: older.toISOString(), session_id: "older-session", user_id: null },
    { archived: false, created_at: recent.toISOString(), session_id: "recent-session", user_id: null },
    { archived: false, created_at: today.toISOString(), session_id: "today-session", user_id: null },
  ];

  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, {
      archiveSession: () => undefined,
      createSession: () => undefined,
      currentSessionId: null,
      isLoadingSessions: false,
      loadCurrentSessionMessages: () => undefined,
      onToggleArchived: () => undefined,
      requestError: null,
      selectSession: () => undefined,
      sessions: groupedSessions,
      showArchived: false,
    }),
  );

  // 三组标题均渲染,会话项保留在各自组内
  assert.match(markup, /data-slot="session-group-today"/);
  assert.match(markup, /data-slot="session-group-recent"/);
  assert.match(markup, /data-slot="session-group-older"/);
  assert.match(markup, /今天/);
  assert.match(markup, /近 7 天/);
  assert.match(markup, /更早/);
  assert.match(markup, /today-session/);
  assert.match(markup, /recent-session/);
  assert.match(markup, /older-session/);
});

test("the sidebar omits section titles for empty groups", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  // 只有「更早」样本:今天 / 近 7 天标题不渲染,仅出现「更早」标题
  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, {
      archiveSession: () => undefined,
      createSession: () => undefined,
      currentSessionId: null,
      isLoadingSessions: false,
      loadCurrentSessionMessages: () => undefined,
      onToggleArchived: () => undefined,
      requestError: null,
      selectSession: () => undefined,
      sessions: [
        { archived: false, created_at: "2020-01-01T00:00:00Z", session_id: "old-session", user_id: null },
      ],
      showArchived: false,
    }),
  );

  assert.match(markup, /data-slot="session-group-older"/);
  assert.doesNotMatch(markup, /data-slot="session-group-today"/);
  assert.doesNotMatch(markup, /data-slot="session-group-recent"/);
});

// D5-T3:加载态骨架屏 ————————————————————————————————
test("the sidebar shows session skeletons while loading and keeps the empty state", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  const baseProps = {
    archiveSession: () => undefined,
    createSession: () => undefined,
    currentSessionId: null,
    isLoadingSessions: true,
    loadCurrentSessionMessages: () => undefined,
    onToggleArchived: () => undefined,
    requestError: null,
    selectSession: () => undefined,
    showArchived: false,
  };

  // 加载态渲染 3 条会话行骨架
  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, { ...baseProps, sessions: [] }),
  );
  assert.equal(
    (markup.match(/data-slot="session-skeleton"/g) ?? []).length,
    3,
  );
  // 三态互斥:加载态不出现「暂无会话」空态文案
  assert.doesNotMatch(markup, /暂无会话/);
  assert.doesNotMatch(markup, /正在加载会话/);
});

test("the sidebar loading branch goes through the Skeleton component in source", () => {
  // review 防回归:isLoadingSessions 分支必须渲染 Skeleton 骨架,
  // 不得回归为「正在加载会话…」文案
  const source = readFileSync(
    new URL("../components/session-sidebar.tsx", import.meta.url),
    "utf-8",
  );
  assert.match(source, /isLoadingSessions \?[\s\S]*?<Skeleton /);
  assert.doesNotMatch(source, /正在加载会话/);
});

// UX-20260808#1:会话标题 ————————————————————————————————
test("filterSessions also matches session titles case-insensitively", async () => {
  const { filterSessions } = await loadSessionSidebar();

  const titledSessions = [
    { archived: false, created_at: "2025-01-01T00:00:00Z", session_id: "session-abc-1", user_id: null, title: "什么是注意力机制" },
    { archived: false, created_at: "2025-01-02T00:00:00Z", session_id: "session-def-2", user_id: null, title: "Backprop 入门" },
  ];

  // 标题命中
  assert.deepEqual(
    filterSessions(titledSessions, "注意力").map((session) => session.session_id),
    ["session-abc-1"],
  );
  // 标题大小写不敏感
  assert.deepEqual(
    filterSessions(titledSessions, "backprop").map((session) => session.session_id),
    ["session-def-2"],
  );
  // session_id 仍可搜(标题 + ID 双字段匹配)
  assert.deepEqual(
    filterSessions(titledSessions, "def").map((session) => session.session_id),
    ["session-def-2"],
  );
});

test("the sidebar renders the session title and a readable legacy fallback", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, {
      archiveSession: () => undefined,
      createSession: () => undefined,
      currentSessionId: null,
      isLoadingSessions: false,
      loadCurrentSessionMessages: () => undefined,
      onToggleArchived: () => undefined,
      requestError: null,
      selectSession: () => undefined,
      sessions: [
        // 有标题:正文展示标题,完整 session_id 收进悬浮提示(title 属性)
        { archived: false, created_at: "2025-01-01T00:00:00Z", session_id: "titled-session-id", user_id: null, title: "机器学习学习路径" },
        // 无标题(存量老会话,契约为 null):正文显示可读占位 + 短 ID
        { archived: false, created_at: "2025-01-02T00:00:00Z", session_id: "legacy-session-id", user_id: null, title: null },
      ],
      showArchived: false,
    }),
  );

  assert.match(markup, /机器学习学习路径/);
  assert.match(markup, /title="titled-session-id"/);
  // 有标题时正文不再把 session_id 当展示文本
  assert.doesNotMatch(markup, />titled-session-id</);
  assert.match(markup, />未命名会话</);
  assert.match(markup, /legacy-s/);
  assert.doesNotMatch(markup, />legacy-session-id</);
});

test("the sidebar renders a compact rail when collapsed", async () => {
  const { SessionSidebarContent } = await loadSessionSidebar();

  const markup = renderToStaticMarkup(
    createElement(SessionSidebarContent, {
      archiveSession: () => undefined,
      collapsed: true,
      createSession: () => undefined,
      currentSessionId: null,
      isLoadingSessions: false,
      loadCurrentSessionMessages: () => undefined,
      onToggleArchived: () => undefined,
      onToggleCollapsed: () => undefined,
      requestError: null,
      selectSession: () => undefined,
      sessions: [],
      showArchived: false,
    }),
  );

  assert.match(markup, /data-slot="sidebar-collapsed"/);
  assert.match(markup, /aria-label="展开会话侧栏"/);
  assert.match(markup, /aria-label="新建会话"/);
  assert.doesNotMatch(markup, /data-slot="session-search"/);
});
