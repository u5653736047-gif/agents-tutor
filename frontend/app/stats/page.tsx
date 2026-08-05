"use client";

// D6-T7:学习进度仪表盘(基础统计版)。
// 独立客户端页面,不接 chat-store(与 knowledge 页同一隔离哲学),直接
// 调 api-client.getStatsOverview:
//   1. 挂载时拉取一次——React 官方数据拉取模式:effect 内局部 async
//      函数 + ignore 标志,setState 全部在 await 之后的异步回调里
//      (react-hooks lint 只拦 effect 同步体内的 setState);
//   2. 空数据不报错:后端返回全 0 时照常渲染 0 值卡片,附「暂无学习
//      数据」提示行;
//   3. 错误(ApiClientError)归一为文案,显示错误行,数据区不渲染;
//   4. SSR 安全:初始 stats=null → 加载骨架,不渲染数据区;最近活动
//      时间原样显示 ISO 字符串,不做本地时区格式化——避免 SSR 与
//      客户端 Date 输出不一致引发 hydration mismatch;
//   5. 「进度分析(错题/知识图谱)」是占位卡:依赖后端错题记录与知识
//      图谱能力,本期未实现,静态渲染并标注「待后端能力」。
import Link from "next/link";
import { useEffect, useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import type { AgentRole } from "@/lib/agent-roles";
import { ApiClientError, apiClient, type StatsOverview } from "@/lib/api-client";

// 角色展示顺序固定(与 core 角色语义一致):督导 → 助教 → 助学 → 评价。
const AGENT_ROLES: AgentRole[] = [
  "supervisor",
  "teaching_assistant",
  "learning_assistant",
  "evaluator",
];

// Agent 回答分布的中文名:键来自契约 AgentRole(与 lib/agent-roles 的
// 徽章 label 各自维护——那边 supervisor 显示英文「Supervisor」,本页
// 统一用中文口径,不动既有组件)。
const ROLE_LABELS: Record<AgentRole, string> = {
  supervisor: "督导",
  teaching_assistant: "助教",
  learning_assistant: "助学",
  evaluator: "评价",
};

// 错误归一:ApiClientError 直接展示后端文案,其余未知错误兜底。
function errorText(error: unknown): string {
  if (error instanceof ApiClientError) {
    return error.message;
  }
  return "请求失败,请稍后重试。";
}

export default function StatsPage() {
  const [stats, setStats] = useState<StatsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 挂载拉取(React 官方数据拉取模式):effect 内局部 async 函数,
  // setState 全在 await 后;ignore 标志防止卸载后 setState(与
  // knowledge 页挂载拉取同构)。
  useEffect(() => {
    let ignore = false;
    async function load() {
      try {
        const overview = await apiClient.getStatsOverview();
        if (ignore) {
          return;
        }
        setStats(overview);
      } catch (caught) {
        if (ignore) {
          return;
        }
        setError(errorText(caught));
      }
    }
    void load();
    return () => {
      ignore = true;
    };
  }, []);

  // 柱状条宽度基准:全 0 时取 1,避免除零(条宽 0%)。
  const distributionCounts = stats
    ? AGENT_ROLES.map((role) => stats.agent_answer_counts[role] ?? 0)
    : [];
  const maxCount = Math.max(...distributionCounts, 1);
  const isEmpty = stats !== null && stats.session_count === 0 && stats.message_count === 0;

  return (
    <main className="mx-auto max-w-3xl px-8 py-6" data-slot="stats-page">
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <p className="text-caption font-medium text-primary">阶段三 · 学习进度</p>
          <h1 className="text-title font-semibold text-foreground">学习进度</h1>
        </div>
        <Link
          className="text-caption text-muted-foreground hover:text-foreground"
          data-slot="stats-back"
          href="/"
        >
          返回首页
        </Link>
      </div>

      {/* 三态:加载骨架 → 错误行 → 数据区;初始(SSR)只渲染骨架 */}
      {stats === null && error === null ? (
        <div
          aria-label="加载中"
          className="mt-6 space-y-3"
          data-slot="stats-loading"
          role="status"
        >
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-24 w-full" />
        </div>
      ) : error ? (
        <div
          className="mt-6 flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-body text-foreground"
          data-slot="stats-error"
          role="alert"
        >
          {error}
        </div>
      ) : stats ? (
        <>
          {/* 统计卡片:会话数 / 消息数 / 最近活动时间(空数据也照常渲染 0) */}
          <section
            aria-label="学习进度统计"
            className="mt-6 grid gap-4 sm:grid-cols-3"
            data-slot="stats-cards"
          >
            <div
              className="rounded-lg border border-border bg-card p-5"
              data-slot="stat-card-sessions"
            >
              <p className="text-caption font-medium text-muted-foreground">会话数</p>
              <p className="mt-2 text-title font-semibold text-foreground">
                {stats.session_count}
              </p>
            </div>
            <div
              className="rounded-lg border border-border bg-card p-5"
              data-slot="stat-card-messages"
            >
              <p className="text-caption font-medium text-muted-foreground">消息数</p>
              <p className="mt-2 text-title font-semibold text-foreground">
                {stats.message_count}
              </p>
            </div>
            <div
              className="rounded-lg border border-border bg-card p-5"
              data-slot="stat-card-last-activity"
            >
              <p className="text-caption font-medium text-muted-foreground">最近活动</p>
              {/* 原样显示 ISO 字符串:本地时区格式化会引发 SSR/客户端
                  hydration mismatch(见组件头注释 4) */}
              <p className="mt-2 text-body font-medium text-foreground">
                {stats.last_activity_at ?? "—"}
              </p>
            </div>
          </section>

          {/* 空数据提示:全 0 时不报错,照常渲染卡片并给出引导文案 */}
          {isEmpty ? (
            <p
              className="mt-4 rounded-md border border-dashed border-border px-3 py-4 text-center text-caption text-muted-foreground"
              data-slot="stats-empty"
            >
              暂无学习数据,开始对话后将在这里展示进度。
            </p>
          ) : null}

          {/* Agent 回答分布:柱状条(角色中文名 + 计数),宽度按最大计数归一 */}
          <section
            aria-label="Agent 回答分布"
            className="mt-6 rounded-lg border border-border bg-card p-5"
            data-slot="stats-agent-distribution"
          >
            <h2 className="text-body font-semibold text-foreground">Agent 回答分布</h2>
            <div className="mt-4 space-y-3">
              {AGENT_ROLES.map((role, index) => {
                const count = distributionCounts[index] ?? 0;
                const width = count === 0 ? 0 : (count / maxCount) * 100;
                return (
                  <div
                    className="flex items-center gap-3"
                    data-slot="stats-agent-bar"
                    key={role}
                  >
                    <span className="w-12 shrink-0 text-caption text-muted-foreground">
                      {ROLE_LABELS[role]}
                    </span>
                    <div className="h-3 flex-1 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-primary"
                        style={{ width: `${width}%` }}
                      />
                    </div>
                    <span className="w-8 shrink-0 text-right text-caption font-medium text-foreground">
                      {count}
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        </>
      ) : null}

      {/* 占位卡:进度分析(错题/知识图谱)——依赖后端错题记录与知识
          图谱能力,本期未实现;静态渲染,不请求任何接口 */}
      <section
        aria-label="进度分析"
        className="mt-6 rounded-lg border border-dashed border-border bg-card p-5"
        data-slot="stats-analysis-placeholder"
      >
        <h2 className="text-body font-semibold text-foreground">进度分析(错题/知识图谱)</h2>
        <p className="mt-2 text-caption text-muted-foreground">
          待后端能力:错题记录与知识点掌握图谱上线后,在这里展示深度分析。
        </p>
      </section>
    </main>
  );
}
