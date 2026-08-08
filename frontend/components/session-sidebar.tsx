"use client";

import { Archive, Plus } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessageFor } from "@/lib/error-messages";
import type { ApiClientError, Session } from "@/lib/api-client";
// D4-T7:会话时间分组(今天 / 近 7 天 / 更早)——纯函数,与组件解耦可测
import { groupSessions, sessionGroupLabel, type SessionGroup } from "@/lib/session-groups";
import { useChatStore } from "@/stores/chat-store";

// D4-T1:会话搜索——过滤与高亮抽为纯函数,组件与测试共用。
// 过滤只匹配 session_id,不区分大小写;空查询(含纯空白)返回全部。
export function filterSessions(sessions: Session[], query: string): Session[] {
  const keyword = query.trim().toLowerCase();
  if (!keyword) {
    return sessions;
  }

  return sessions.filter((session) =>
    session.session_id.toLowerCase().includes(keyword),
  );
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
  requestError: ApiClientError | null;
  selectSession: (sessionId: string) => void;
  sessions: Session[];
  showArchived: boolean;
};

export function SessionSidebarContent({
  archiveSession,
  createSession,
  currentSessionId,
  isLoadingSessions,
  loadCurrentSessionMessages,
  onToggleArchived,
  onRetrySessions,
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
  // D4-T7:过滤后再按 created_at 分组(今天 / 近 7 天 / 更早);组内保持
  // 输入顺序,空组不渲染标题。
  const groups = groupSessions(visibleSessions);
  // 仅在有实际查询词(trim 后非空)时展示「未找到」占位
  const hasQuery = effectiveQuery.trim() !== "";
  // D2-T5:请求错误统一映射为标题 + 说明(覆盖 ApiErrorCode 与网络失败)
  const requestErrorPreset = requestError
    ? errorMessageFor(requestError.code)
    : null;

  return (
    <aside
      className="flex min-h-screen w-72 flex-col border-r border-border bg-card"
      data-slot="session-sidebar"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-4">
        <div>
          <p className="text-caption font-medium text-muted-foreground">协作式 Agent</p>
          <h1 className="text-body font-semibold text-foreground">会话</h1>
        </div>
        <Button
          aria-label="新建会话"
          className="gap-2"
          onClick={() => void createSession()}
          size="sm"
          type="button"
        >
          <Plus aria-hidden className="size-4" />
          新建会话
        </Button>
      </div>

      {/* D4-T1:会话搜索框。输入防抖 200ms 后过滤;清空输入立即恢复全列表。 */}
      <div className="border-b border-border px-4 py-3">
        <input
          aria-label="搜索会话"
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-caption text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/40"
          data-slot="session-search"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索会话 ID…"
          type="search"
          value={query}
        />
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

        {/* D4-T7:会话按 created_at 分组展示——组标题置顶,组内保持输入
            顺序;空组不渲染标题(含标题)。搜索态同样按组展示
            (visibleSessions 已过滤)。 */}
        {GROUP_ORDER.map((group) => {
          const groupItems = groups[group];
          if (groupItems.length === 0) {
            return null;
          }

          return (
            <div key={group}>
              <p
                className="px-2 pb-1 pt-3 text-caption font-medium text-muted-foreground"
                data-slot={`session-group-${group}`}
              >
                {sessionGroupLabel(group)}
              </p>
              {groupItems.map((session) => {
                const selected = session.session_id === currentSessionId;

                return (
                  <div
                    className={
                      selected
                        ? // UX-20260807#4:选中态品牌化——品牌蓝 10% 底 +
                          // 25% 环,替代灰底(全页唯一选中态不再被灰淹没)
                          "group mb-1 flex items-center rounded-md bg-primary/10 px-3 py-2 ring-1 ring-primary/25"
                        : "group mb-1 flex items-center rounded-md px-3 py-2 hover:bg-muted/60"
                    }
                    key={session.session_id}
                  >
                    <button
                      className="min-w-0 flex-1 truncate text-left text-caption font-medium text-foreground"
                      onClick={() => {
                        selectSession(session.session_id);
                        void loadCurrentSessionMessages();
                      }}
                      type="button"
                    >
                      {/* D4-T1:命中片段用 <mark> 高亮(定位不区分大小写,展示保留
                          原始大小写);未命中时整段以普通 span 渲染。用
                          effectiveQuery 而非 debounced(review 修正):清空查询后
                          高亮随列表一起立即消失,防抖等待期高亮与列表一致。 */}
                      {highlightMatch(session.session_id, effectiveQuery).map(
                        (segment, index) =>
                          segment.highlighted ? (
                            // UX-20260807#4:高亮色走语义 warning token
                            // (替代 amber 硬编码,两模式自动适配);
                            // text-inherit 必须保留,否则暗色回退黑字。
                            <mark className="bg-warning/20 text-inherit" key={index}>
                              {segment.text}
                            </mark>
                          ) : (
                            <span key={index}>{segment.text}</span>
                          ),
                      )}
                    </button>
                    {/* D4-T7 review nit:归档视图下会话已是归档态,归档按钮
                        是无效操作——仅未归档视图显示。 */}
                    {!showArchived ? (
                      <button
                        aria-label={`归档会话 ${session.session_id}`}
                        className="ml-2 inline-flex size-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-background hover:text-foreground"
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
        className="flex w-full items-center justify-center border-t border-border px-4 py-3 text-caption font-medium text-muted-foreground hover:bg-muted/60 hover:text-foreground"
        data-slot="archive-toggle"
        onClick={onToggleArchived}
        type="button"
      >
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
  onSessionSelected?: () => void;
};

export function SessionSidebar({ onSessionSelected }: SessionSidebarProps = {}) {
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
      createSession={createSession}
      currentSessionId={currentSessionId}
      isLoadingSessions={isLoadingSessions}
      loadCurrentSessionMessages={loadCurrentSessionMessages}
      onToggleArchived={() => setShowArchived(!showArchived)}
      onRetrySessions={() => void refreshSessions()}
      requestError={requestError}
      selectSession={handleSelectSession}
      sessions={sessions}
      showArchived={showArchived}
    />
  );
}
