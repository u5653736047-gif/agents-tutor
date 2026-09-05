"use client";

// D6-T7:学习进度仪表盘(基础统计版) + 赛前可视化增强(学情洞察卡)。
// 独立客户端页面,不接 chat-store(与 knowledge 页同一隔离哲学),直接
// 调 api-client:
//   1. 挂载时拉取一次——React 官方数据拉取模式:effect 内局部 async
//      函数 + ignore 标志,setState 全部在 await 之后的异步回调里
//      (react-hooks lint 只拦 effect 同步体内的 setState);
//   2. 三源拉取:基础统计(/stats/overview)失败 → 错误行;诊断与洞察
//      (/learning/*)是增强数据,失败降级为「暂不可用」提示行,不击穿
//      基础统计卡(降级优先级与端点侧「空报告 200」红线呼应);
//   3. 空数据不报错:后端返回全 0 时照常渲染 0 值卡片,附「暂无学习
//      数据」提示行;
//   4. SSR 安全:初始 stats=null → 加载骨架,不渲染数据区;时间字符串
//      原样显示,不做本地时区格式化——避免 SSR 与客户端 Date 输出不
//      一致引发 hydration mismatch;
//   5. 学情洞察四卡(替换旧占位卡):薄弱点预警(诊断端点)、错题归因
//      分布、正确率趋势、学习路径回显(洞察端点)——全部是后端确定性
//      SQL 聚合的只读视图,前端零计算口径(仅条宽归一)。
import Link from "next/link";
import { useEffect, useState } from "react";

import { AiContentNotice } from "@/components/ai-content-notice";
import { Skeleton } from "@/components/ui/skeleton";
import type { AgentRole } from "@/lib/agent-roles";
import {
  ApiClientError,
  apiClient,
  type DiagnosisSummary,
  type LearningInsights,
  type StatsOverview,
} from "@/lib/api-client";

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

// 百分比文案:契约 accuracy 为 0-1 小数,展示统一一位小数百分比。
function accuracyPercent(accuracy: number): string {
  return `${Math.round(accuracy * 100)}%`;
}

// ISO 时间戳截取日期段原样展示(不做时区转换,避免 hydration 差异)。
function dateOnly(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  return value.slice(0, 10);
}

export default function StatsPage() {
  const [stats, setStats] = useState<StatsOverview | null>(null);
  const [diagnosis, setDiagnosis] = useState<DiagnosisSummary | null>(null);
  const [insights, setInsights] = useState<LearningInsights | null>(null);
  // 诊断与洞察双双失败才整体标注「暂不可用」——单侧失败时另一侧
  // 卡片照常渲染(两卡数据源独立)。
  const [insightsFailed, setInsightsFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 挂载拉取(React 官方数据拉取模式):effect 内局部 async 函数,
  // setState 全在 await 后;ignore 标志防止卸载后 setState。
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
        return;
      }
      // 增强数据用 allSettled 拉取:任一成功照常渲染,全败才降级提示。
      const [diag, ins] = await Promise.allSettled([
        apiClient.getDiagnosisSummary(),
        apiClient.getLearningInsights(),
      ]);
      if (ignore) {
        return;
      }
      if (diag.status === "fulfilled") {
        setDiagnosis(diag.value);
      }
      if (ins.status === "fulfilled") {
        setInsights(ins.value);
      }
      if (diag.status === "rejected" && ins.status === "rejected") {
        setInsightsFailed(true);
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

  // 错题归因条数据:契约 JSON 保序(后端已按计数倒序),条宽按最大计数归一。
  // 契约列表字段为可选(--default-non-nullable=false),读取端宽容 ?? []。
  const errorTagEntries = insights
    ? Object.entries(insights.error_tag_counts ?? {})
    : [];
  const maxTagCount = Math.max(...errorTagEntries.map(([, count]) => count), 1);
  // 正确率趋势条:后端升序,条长按正确率,标签取日期段。
  const dailyPoints = insights?.daily_accuracy ?? [];
  const pathPlans = insights?.recent_path_plans ?? [];
  const weakPoints = diagnosis?.weak_points ?? [];
  const knowledgePoints = diagnosis?.knowledge_points ?? [];

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

          {/* 学情洞察区(赛前可视化增强):四卡数据全部来自后端确定性
              聚合(/learning/diagnosis/summary + /learning/insights/summary)。
              拉取双败时整体降级为一行提示,不渲染空卡。 */}
          <section
            aria-label="学情洞察"
            className="mt-6 rounded-lg border border-border bg-card p-5"
            data-slot="stats-insights"
          >
            <h2 className="text-body font-semibold text-foreground">学情洞察</h2>
            {insightsFailed ? (
              <p
                className="mt-3 text-caption text-muted-foreground"
                data-slot="stats-insights-unavailable"
              >
                洞察数据暂不可用,请稍后重试。
              </p>
            ) : (
              <div className="mt-4 grid gap-6">
                {/* 薄弱点预警:诊断端点规则(作答≥2 次且加权正确率<0.6) */}
                <div data-slot="stats-weak-points">
                  <h3 className="text-caption font-medium text-muted-foreground">
                    薄弱知识点预警
                  </h3>
                  {diagnosis === null ? (
                    <p className="mt-2 text-caption text-muted-foreground">加载中…</p>
                  ) : weakPoints.length === 0 ? (
                    <p
                      className="mt-2 text-caption text-muted-foreground"
                      data-slot="stats-weak-empty"
                    >
                      {diagnosis.total_attempts === 0
                        ? "暂无作答记录,完成练习或批改后生成预警。"
                        : "当前无薄弱知识点预警,继续保持。"}
                    </p>
                  ) : (
                    <ul className="mt-2 flex flex-col gap-1.5">
                      {weakPoints.map((point) => {
                        const detail = knowledgePoints.find(
                          (item) => item.knowledge_point === point,
                        );
                        return (
                          <li
                            className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted/40 px-3 py-2"
                            data-slot="stats-weak-item"
                            key={point}
                          >
                            <span className="min-w-0 truncate text-caption text-foreground">
                              {point}
                            </span>
                            <span className="shrink-0 text-caption font-medium text-destructive">
                              {detail ? accuracyPercent(detail.accuracy) : "—"}
                            </span>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>

                {/* 错题归因分布:错因标签计数条(宽度按最大计数归一) */}
                <div data-slot="stats-error-tags">
                  <h3 className="text-caption font-medium text-muted-foreground">
                    错题归因分布
                  </h3>
                  {insights === null ? (
                    <p className="mt-2 text-caption text-muted-foreground">加载中…</p>
                  ) : insights.total_wrong === 0 ? (
                    <p
                      className="mt-2 text-caption text-muted-foreground"
                      data-slot="stats-error-tags-empty"
                    >
                      暂无错题记录。
                    </p>
                  ) : (
                    <div className="mt-2 space-y-2">
                      {errorTagEntries.map(([tag, count]) => (
                        <div
                          className="flex items-center gap-3"
                          data-slot="stats-error-tag-bar"
                          key={tag}
                        >
                          <span className="w-20 shrink-0 truncate text-caption text-muted-foreground">
                            {tag}
                          </span>
                          <div className="h-3 flex-1 overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-primary"
                              style={{ width: `${(count / maxTagCount) * 100}%` }}
                            />
                          </div>
                          <span className="w-8 shrink-0 text-right text-caption font-medium text-foreground">
                            {count}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 正确率趋势:按日加权正确率条(长度=正确率,灰底=作答量相对值) */}
                <div data-slot="stats-accuracy-trend">
                  <h3 className="text-caption font-medium text-muted-foreground">
                    正确率趋势
                  </h3>
                  {insights === null ? (
                    <p className="mt-2 text-caption text-muted-foreground">加载中…</p>
                  ) : dailyPoints.length === 0 ? (
                    <p
                      className="mt-2 text-caption text-muted-foreground"
                      data-slot="stats-trend-empty"
                    >
                      暂无作答趋势数据。
                    </p>
                  ) : (
                    <div className="mt-2 space-y-2">
                      {dailyPoints.map((point) => (
                        <div
                          className="flex items-center gap-3"
                          data-slot="stats-trend-bar"
                          key={point.date}
                        >
                          <span className="w-20 shrink-0 text-caption text-muted-foreground">
                            {point.date.slice(5)}
                          </span>
                          <div className="h-3 flex-1 overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-primary"
                              style={{ width: `${point.accuracy * 100}%` }}
                            />
                          </div>
                          <span className="w-20 shrink-0 text-right text-caption font-medium text-foreground">
                            {accuracyPercent(point.accuracy)} · {point.attempts}题
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 学习路径回显:最近的路径存档记录(知识点 + 日期) */}
                <div data-slot="stats-path-plans">
                  <h3 className="text-caption font-medium text-muted-foreground">
                    最近学习路径
                  </h3>
                  {insights === null ? (
                    <p className="mt-2 text-caption text-muted-foreground">加载中…</p>
                  ) : pathPlans.length === 0 ? (
                    <p
                      className="mt-2 text-caption text-muted-foreground"
                      data-slot="stats-path-empty"
                    >
                      暂无路径记录,在对话中请求「规划学习路径」后在这里回显。
                    </p>
                  ) : (
                    <ul className="mt-2 flex flex-col gap-1.5">
                      {pathPlans.map((plan, index) => (
                        <li
                          className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted/40 px-3 py-2"
                          data-slot="stats-path-item"
                          key={`${plan.knowledge_point ?? "未分类"}-${index}`}
                        >
                          <span className="min-w-0 truncate text-caption text-foreground">
                            {plan.knowledge_point ?? "未分类知识点"}
                          </span>
                          <span className="shrink-0 text-caption text-muted-foreground">
                            {dateOnly(plan.created_at)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            )}
          </section>
        </>
      ) : null}

      {/* AI 生成内容标识(伦理合规):页面级全局声明 */}
      <AiContentNotice variant="footer" />
    </main>
  );
}
