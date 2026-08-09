"use client";

import {
  Archive,
  ChevronsLeft,
  ChevronsRight,
  Plus,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessageFor } from "@/lib/error-messages";
import type { ApiClientError, Session } from "@/lib/api-client";
// D4-T7:会话时间分组(今天 / 近 7 天 / 更早)——纯函数,与组件解耦可测
import { groupSessions, sessionGroupLabel, type SessionGroup } from "@/lib/session-groups";
import { useChatStore } from "@/stores/chat-store";

// D4-T1:会话搜索——过滤与高亮抽为纯函数,组件与测试共用。
// UX-20260808#1:过滤同时匹配标题与 session_id(不区分大小写);
// 标题为可选(存量老会话为 null),空查询(含纯空白)返回全部。
export function filterSessions(sessions: Session[], query: string): Session[] {
  const keyword = query.trim().toLowerCase();
  if (!keyword) {
    return sessions;
  }

  return sessions.filter((session) =>
    [session.title ?? "", session.session_id].some((text) =>
      text.toLowerCase().includes(keyword),
    ),
  );
}

export function sessionDisplayTitle(session: Session): string {
  return session.title?.trim() || "未命名会话";
}

export function shortSessionId(sessionId: string): string {
  return sessionId.slice(0, 8);
}

// D4-T1:命中高亮切分。返回三段(前缀 / 命中 / 后缀);query 为空或未
// 命中时返回单段 non-highlighted。定位用全小写,切分用原始 session_id,
// 因此高亮保留原始大小写。
export type HighlightSegment = {
  highlighted: boolean;
  text: string;
};

export function highlightMatch(
  sessionId: string,
  query: string,
): HighlightSegment[] {
  if (!query) {
    return [{ highlighted: false, text: sessionId }];
  }

  const start = sessionId.toLowerCase().indexOf(query.toLowerCase());
  if (start === -1) {
    return [{ highlighted: false, text: sessionId }];
  }

  const end = start + query.length;
  return [
    { highlighted: false, text: sessionId.slice(0, start) },
    { highlighted: true, text: sessionId.slice(start, end) },
    { highlighted: false, text: sessionId.slice(end) },
  ];
}

// D4-T7:分组渲染顺序(今天 → 近 7 天 → 更早),空组不渲染标题。
const GROUP_ORDER: SessionGroup[] = ["today", "recent", "older"];

// D4-T1:展示组件接收 props(纯函数 + SSR 可测);顶部容器 SessionSidebar
// 从 store 订阅后原样转发。交互(选中/归档/新建)只依赖 props 回调,
// 测试用 stub 注入即可,无需触碰 zustand store。
type SessionSidebarContentProps = {
  archiveSession: (sessionId: string) => void;
  collapsed?: boolean;
  createSession: () => void;
  currentSessionId: string | null;
  isLoadingSessions: boolean;
  loadCurrentSessionMessages: () => void;
  // D4-T7:归档视图开关——true 时列表为归档会话(store 按此拉取),
  // 按钮文案与空态随之切换。
  onToggleArchived: () => void;
  // D8 修复:错误块「重试」回调——断网/后端重启等请求失败后提供恢复
  // 入口(容器转发 store.refreshSessions,重试前自动清 requestError)。
  // 可选:未提供(既有测试注入)时不渲染按钮,行为不变。
  onRetrySessions?: () => void;
  onClose?: () => void;
  onToggleCollapsed?: () => void;
  requestError: ApiClientError | null;
  selectSession: (sessionId: string) => void;
  sessions: Session[];
  showArchived: boolean;
};

export function SessionSidebarContent({
  archiveSession,
  collapsed = false,
  createSession,
  currentSessionId,
  isLoadingSessions,
  loadCurrentSessionMessages,
  onToggleArchived,
  onRetrySessions,
  onClose,
  onToggleCollapsed,
  requestError,
  selectSession,
  sessions,
  showArchived,
}: SessionSidebarContentProps) {
  // D4-T1:搜索防抖——query 即时更新(输入框受控值),debounced 延迟
  // 200ms 生效用于过滤;清空输入时立即恢复全列表(不等防抖)。
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    // 防抖 200ms。注意不在 effect 内同步 setState(eslint react-hooks
    // 禁止「effect 内同步 setState 引发级联渲染」);query 清空的即时
    // 生效由下方 effectiveQuery 处理(清空时直接回落空串,不等防抖)。
    if (query === "") {
      return;
    }
    const timer = setTimeout(() => setDebounced(query), 200);
    return () => clearTimeout(timer);
  }, [query]);

  // query 清空(或纯空白)时立即回落空串恢复全列表;否则用防抖后的词。
  const effectiveQuery = query.trim() === "" ? "" : debounced;
  const visibleSessions = filterSessions(sessions, effectiveQuery);
  // 过滤后按最后活跃时间分组并倒序，空组不渲染标题。
  const groups = groupSessions(visibleSessions);
  // 仅在有实际查询词(trim 后非空)时展示「未找到」占位
  const hasQuery = effectiveQuery.trim() !== "";
  // D2-T5:请求错误统一映射为标题 + 说明(覆盖 ApiErrorCode 与网络失败)
  const requestErrorPreset = requestError
    ? errorMessageFor(requestError.code)
    : null;

  if (collapsed) {
    return (
      <aside
        className="flex h-dvh w-full flex-col items-center border-r border-border/70 bg-card/90 py-3"
        data-slot="session-sidebar"
      >
        <div className="flex w-full flex-col items-center gap-2" data-slot="sidebar-collapsed">
          <div className="mb-1 flex size-9 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <Sparkles aria-hidden className="size-4" />
          </div>
          <button
            aria-label="展开会话侧栏"
            className="inline-flex size-9 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            data-slot="sidebar-collapse"
            onClick={onToggleCollapsed}
            type="button"
          >
            <ChevronsRight aria-hidden className="size-4" />
          </button>
          <button
            aria-label="新建会话"
            className="inline-flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-sm transition-colors hover:bg-primary/90"
            onClick={() => void createSession()}
            type="button"
          >
            <Plus aria-hidden className="size-4" />
          </button>
        </div>
      </aside>
    );
  }

  return (
    <aside
      // UX-20260808#2:min-h-screen → h-dvh——固定视口高度,内部
      // 「flex-1 overflow-y-auto」的会话列表才真正成为独立滚动容器;
      // 桌面(grid 行高=视口)与移动端抽屉(inset-y-0)两种挂载都成立。
      className="flex h-dvh w-full flex-col border-r border-border/70 bg-card/90"
      data-slot="session-sidebar"
    >
      <div className="border-b border-border/70 p-4">
        <div className="flex items-center gap-3">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow-sm">
            <Sparkles aria-hidden className="size-4" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-body font-semibold text-foreground">AI 学习助理</h1>
            <p className="truncate text-caption text-muted-foreground">多智能体协作空间</p>
          </div>
          {onToggleCollapsed ? (
            <button
              aria-label="收起会话侧栏"
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              data-slot="sidebar-collapse"
              onClick={onToggleCollapsed}
              type="button"
            >
              <ChevronsLeft aria-hidden className="size-4" />
            </button>
          ) : null}
          {onClose ? (
            <button
              aria-label="关闭会话侧栏"
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              onClick={onClose}
              type="button"
            >
              <X aria-hidden className="size-4" />
            </button>
          ) : null}
        </div>
        <Button
          aria-label="新建会话"
          className="mt-4 w-full gap-2"
          onClick={() => void createSession()}
          size="sm"
          type="button"
        >
          <Plus aria-hidden className="size-4" />
          新建会话
        </Button>
      </div>

      {/* D4-T1:会话搜索框。输入防抖 200ms 后过滤;清空输入立即恢复全列表。 */}
      <div className="border-b border-border/70 px-4 py-3">
        <div className="relative">
          <Search
            aria-hidden
            className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <input
            aria-label="搜索会话"
            className="w-full rounded-lg border border-border bg-background/70 py-2 pl-9 pr-3 text-caption text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/30"
            data-slot="session-search"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索会话"
            type="search"
            value={query}
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-2">
        {/* D5-T3:加载态骨架——3 条会话行占位(左侧方形占位 + 两行灰条,
            行高与真实会话行 rounded-md px-3 py-2 对齐,减少切换跳动);
            isLoadingSessions 结束后三态互斥:有会话渲染列表,无会话走
            下方「暂无会话」空态。review nit:空态加 !hasQuery 守卫——
            「暂无会话」与「未找到匹配的会话」两个空态互斥不同屏。 */}
        {isLoadingSessions ? (
          <div className="space-y-1 px-2 py-1">
            {[0, 1, 2].map((index) => (
              <div
                className="flex items-center gap-3 rounded-md px-3 py-2"
                data-slot="session-skeleton"
                key={index}
              >
                <Skeleton className="size-8 rounded-md" />
                <div className="min-w-0 flex-1 space-y-1.5">
                  <Skeleton className="h-3 w-3/4" />
                  <Skeleton className="h-2.5 w-1/2" />
                </div>
              </div>
            ))}
          </div>
        ) : null}

        {!isLoadingSessions && sessions.length === 0 && !hasQuery ? (
          <p
            className="px-2 py-3 text-caption text-muted-foreground"
            data-slot={showArchived ? "archive-empty" : undefined}
          >
            {/* D4-T7:归档视图下的空态文案与未归档视图区分 */}
            {showArchived ? "暂无归档会话" : "暂无会话"}
          </p>
        ) : null}

        {/* D4-T1:搜索无结果占位——仅在有查询词时出现 */}
        {hasQuery && visibleSessions.length === 0 ? (
          <p
            className="px-2 py-3 text-caption text-muted-foreground"
            data-slot="session-search-empty"
          >
            未找到匹配的会话
          </p>
        ) : null}

        {/* 会话按 updated_at 倒序分组；旧契约回退 created_at。
            空组不渲染标题，搜索态同样按组展示。 */}
        {GROUP_ORDER.map((group) => {
          const groupItems = groups[group];
          if (groupItems.length === 0) {
            return null;
          }

          return (
            <div key={group}>
              <p
                className="sticky top-0 z-10 bg-card/95 px-2 pb-1 pt-3 text-caption font-medium text-muted-foreground backdrop-blur"
                data-slot={`session-group-${group}`}
              >
                {sessionGroupLabel(group)}
              </p>
              {groupItems.map((session) => {
                const selected = session.session_id === currentSessionId;
                const displayTitle = sessionDisplayTitle(session);

                return (
                  <div
                    className={
                      selected
                        ? // UX-20260807#4:选中态品牌化——品牌蓝 10% 底 +
                          // 25% 环,替代灰底(全页唯一选中态不再被灰淹没)
                          "group mb-1 flex items-center rounded-lg bg-primary/10 px-3 py-2.5 ring-1 ring-primary/20"
                        : "group mb-1 flex items-center rounded-lg px-3 py-2.5 transition-colors hover:bg-muted/60"
                    }
                    key={session.session_id}
                  >
                    <button
                      className="min-w-0 flex-1 text-left"
                      onClick={() => {
                        selectSession(session.session_id);
                        void loadCurrentSessionMessages();
                      }}
                      // UX-20260808#1:悬浮提示保留完整 session_id(标题
                      // 截断/重名时仍可区分)
                      title={session.session_id}
                      type="button"
                    >
                      {/* D4-T1:命中片段用 <mark> 高亮(定位不区分大小写,展示保留
                          原始大小写);未命中时整段以普通 span 渲染。用
                          effectiveQuery 而非 debounced(review 修正):清空查询后
                          高亮随列表一起立即消失,防抖等待期高亮与列表一致。
                          无标题的存量会话显示可读占位和短 ID；完整 ID
                          保留在悬浮提示中。 */}
                      <span className="block truncate text-caption font-medium text-foreground">
                        {highlightMatch(displayTitle, effectiveQuery).map(
                          (segment, index) =>
                            segment.highlighted ? (
                              <mark className="bg-warning/20 text-inherit" key={index}>
                                {segment.text}
                              </mark>
                            ) : (
                              <span key={index}>{segment.text}</span>
                            ),
                        )}
                      </span>
                      {!session.title ? (
                        <span className="mt-0.5 block truncate text-[0.6875rem] text-muted-foreground">
                          会话 {shortSessionId(session.session_id)}
                        </span>
                      ) : null}
                    </button>
                    {/* D4-T7 review nit:归档视图下会话已是归档态,归档按钮
                        是无效操作——仅未归档视图显示。 */}
                    {!showArchived ? (
                      <button
                        aria-label={`归档会话 ${session.session_id}`}
                        className="ml-2 inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground opacity-100 transition-[opacity,color,background-color] hover:bg-background hover:text-foreground md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100"
                        onClick={() => void archiveSession(session.session_id)}
                        type="button"
                      >
                        <Archive aria-hidden className="size-4" />
                      </button>
                    ) : null}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>

      {/* D4-T7:归档视图切换。showArchived=true 时列表为归档会话,按钮
          文案反转为「查看未归档」。恢复(取消归档)不在本期范围——core
          SessionStore 无 unarchive 接口(D4-T7 降级口径:归档可查看即可)。 */}
      <button
        className="flex w-full items-center justify-center gap-2 border-t border-border/70 px-4 py-3 text-caption font-medium text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
        data-slot="archive-toggle"
        onClick={onToggleArchived}
        type="button"
      >
        <Archive aria-hidden className="size-4" />
        {showArchived ? "查看未归档" : "查看归档"}
      </button>

      {requestError && requestErrorPreset ? (
        <div
          className="border-t border-border px-4 py-3"
          data-slot="sidebar-request-error"
          role="alert"
        >
          <p className="text-caption font-medium text-destructive">
            {requestErrorPreset.title}
          </p>
          <p className="mt-0.5 text-caption text-muted-foreground">
            {requestErrorPreset.detail}
          </p>
          {/* D8 修复:请求失败(网络/超时/服务错误)后给出显式恢复入口——
              点击重新拉取会话列表(store 的 refreshSessions 重试前自动
              清 requestError)。此前错误块只有文案没有动作,后端短暂
              不可用恢复后必须手动做一次交互才能重试。 */}
          {onRetrySessions ? (
            <button
              className="mt-2 rounded-md border border-border px-3 py-1.5 text-caption font-medium text-foreground hover:bg-muted hover:text-foreground"
              data-slot="sidebar-request-retry"
              onClick={onRetrySessions}
              type="button"
            >
              重试
            </button>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}

// D4-T1:容器——从 store 订阅全部所需状态并转发给展示组件。
// D4-T5:新增可选 prop onSessionSelected(移动端抽屉「选中会话后自动
// 收起」用;桌面分支不传,向后兼容既有调用与测试)。
type SessionSidebarProps = {
  collapsed?: boolean;
  onClose?: () => void;
  onSessionSelected?: () => void;
  onToggleCollapsed?: () => void;
};

export function SessionSidebar({
  collapsed = false,
  onClose,
  onSessionSelected,
  onToggleCollapsed,
}: SessionSidebarProps = {}) {
  const archiveSession = useChatStore((state) => state.archiveSession);
  const createSession = useChatStore((state) => state.createSession);
  const currentSessionId = useChatStore((state) => state.currentSessionId);
  const isLoadingSessions = useChatStore((state) => state.isLoadingSessions);
  const loadCurrentSessionMessages = useChatStore(
    (state) => state.loadCurrentSessionMessages,
  );
  const refreshSessions = useChatStore((state) => state.refreshSessions);
  const requestError = useChatStore((state) => state.requestError);
  const selectSession = useChatStore((state) => state.selectSession);
  const sessions = useChatStore((state) => state.sessions);
  // D4-T7:归档视图开关——容器从 store 订阅并转发,切换时由 store 触发
  // 按新视图重新拉取。
  const setShowArchived = useChatStore((state) => state.setShowArchived);
  const showArchived = useChatStore((state) => state.showArchived);

  useEffect(() => {
    // 挂载时拉取会话列表(review blocking 修复:重构时勿删——store
    // 初始 sessions 为空,不拉取则侧栏永远「暂无会话」)。
    void refreshSessions();
  }, [refreshSessions]);

  // D4-T5:选中会话后回调(抽屉收起);注意点击「当前已选中会话」的
  // 场景也调用(语义为「选择动作」而非「切换」,抽屉收起符合直觉)。
  const handleSelectSession = (sessionId: string) => {
    selectSession(sessionId);
    onSessionSelected?.();
  };

  return (
    <SessionSidebarContent
      archiveSession={archiveSession}
      collapsed={collapsed}
      createSession={createSession}
      currentSessionId={currentSessionId}
      isLoadingSessions={isLoadingSessions}
      loadCurrentSessionMessages={loadCurrentSessionMessages}
      onToggleArchived={() => setShowArchived(!showArchived)}
      onClose={onClose}
      onRetrySessions={() => void refreshSessions()}
      onToggleCollapsed={onToggleCollapsed}
      requestError={requestError}
      selectSession={handleSelectSession}
      sessions={sessions}
      showArchived={showArchived}
    />
  );
}
