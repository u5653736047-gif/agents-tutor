"use client";

// D6-T4:知识库检索测试面板(教师端)。
// 独立客户端页面,不接 chat-store(避免污染主会话状态),直接调
// api-client.searchKnowledge:
//   1. 提交表单 → await searchKnowledge,结果写入 state(再次检索覆盖旧结果);
//   2. 错误(ApiClientError)归一为文案并清空旧结果,避免残留上一次
//      检索内容误导;503 + knowledge_unavailable 给专门提示;
//   3. SSR 安全:初始 loading/error/result 均为空态,不渲染结果区,
//      表单本身可 SSR 直渲;提交与结果渲染发生在 hydration 之后。
import Link from "next/link";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiClientError, apiClient, type KnowledgeSearchResult } from "@/lib/api-client";

// top_k 可选档位 1–10,默认 5(与契约 KnowledgeSearchRequest 默认一致)
const TOP_K_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

// 错误归一:ApiClientError 直接展示后端文案;503 + knowledge_unavailable
// 是知识服务未就绪的稳定错误码,给教师端专门提示;其余未知错误兜底。
function errorText(error: unknown): string {
  if (error instanceof ApiClientError) {
    if (error.code === "knowledge_unavailable") {
      return "知识库暂不可用,请检查后端知识服务。";
    }
    return error.message;
  }
  return "请求失败,请稍后重试。";
}

export default function KnowledgePage() {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmed = query.trim();
    if (!trimmed) {
      return;
    }
    // review nit:防重入——按钮 disabled 不拦截 Enter 隐式提交,
    // 检索中再次提交会并发两个请求(后完成者覆盖先完成者)。
    if (loading) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const searchResult = await apiClient.searchKnowledge({ query: trimmed, topK });
      setResult(searchResult);
    } catch (caught) {
      // 错误清空旧结果(见组件头注释 2)
      setError(errorText(caught));
      setResult(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="mx-auto max-w-3xl px-8 py-6">
      <div className="flex items-baseline justify-between gap-4">
        <div>
          <p className="text-caption font-medium text-primary">教师端</p>
          <h1 className="text-title font-semibold text-foreground">知识库检索测试</h1>
        </div>
        <Link
          className="text-caption text-muted-foreground hover:text-foreground"
          data-slot="knowledge-back"
          href="/"
        >
          返回首页
        </Link>
      </div>

      <form
        className="mt-6 rounded-lg border border-border bg-card p-5"
        data-slot="knowledge-form"
        onSubmit={handleSubmit}
      >
        <label
          className="block text-caption font-medium text-foreground"
          htmlFor="knowledge-query"
        >
          查询内容
        </label>
        <input
          className="mt-2 w-full rounded-md border border-border bg-background px-3 py-2 text-body text-foreground placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
          data-slot="knowledge-query"
          id="knowledge-query"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="输入要检索的知识库问题,如:什么是反向传播?"
          required
          type="text"
          value={query}
        />

        <div className="mt-4 flex items-end justify-between gap-4">
          <div>
            <label
              className="block text-caption font-medium text-foreground"
              htmlFor="knowledge-topk"
            >
              返回条数
            </label>
            <select
              className="mt-2 rounded-md border border-border bg-background px-3 py-2 text-body text-foreground focus-visible:ring-2 focus-visible:ring-ring"
              data-slot="knowledge-topk"
              id="knowledge-topk"
              onChange={(event) => setTopK(Number(event.target.value))}
              value={topK}
            >
              {TOP_K_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </div>
          <Button data-slot="knowledge-search-btn" disabled={loading} type="submit">
            {loading ? "检索中…" : "检索"}
          </Button>
        </div>
      </form>

      {/* 结果区:初始不渲染(null);提交后按 加载/错误/空/列表 四态渲染 */}
      {loading ? (
        <div
          aria-label="检索中"
          className="mt-6 space-y-3"
          data-slot="knowledge-loading"
          role="status"
        >
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : error ? (
        <div
          className="mt-6 flex items-start gap-3 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-body text-foreground"
          data-slot="knowledge-error"
          role="alert"
        >
          {error}
        </div>
      ) : result ? (
        result.hits.length === 0 ? (
          <div
            className="mt-6 rounded-lg border border-border bg-card px-4 py-6 text-center text-body text-muted-foreground"
            data-slot="knowledge-empty"
          >
            未找到相关内容
          </div>
        ) : (
          <ul className="mt-6 space-y-3">
            {result.hits.map((hit, index) => (
              <li
                className="rounded-lg border border-border bg-card p-4"
                data-slot="knowledge-hit"
                key={`${hit.citation.chunk_id}-${index}`}
              >
                <div className="flex items-start justify-between gap-3">
                  {/* summary 后端已截断,前端再以 3 行截断兜底(line-clamp
                      为 Tailwind 内置工具类),超长文本不撑破卡片 */}
                  <p className="line-clamp-3 text-body text-foreground">{hit.summary}</p>
                  <span className="shrink-0 text-caption font-medium text-muted-foreground">
                    {hit.score.toFixed(3)}
                  </span>
                </div>
                <p
                  className="mt-2 text-caption text-muted-foreground"
                  data-slot="knowledge-citation"
                >
                  文档 {hit.citation.document_id} · 来源 {hit.citation.source} · 页码{" "}
                  {hit.citation.page ?? "—"} · 分块 {hit.citation.chunk_id}
                </p>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </main>
  );
}
