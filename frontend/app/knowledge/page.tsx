"use client";

// D6-T4:知识库检索测试面板(教师端)。
// 独立客户端页面,不接 chat-store(避免污染主会话状态),直接调
// api-client.searchKnowledge:
//   1. 提交表单 → await searchKnowledge,结果写入 state(再次检索覆盖旧结果);
//   2. 错误(ApiClientError)归一为文案并清空旧结果,避免残留上一次
//      检索内容误导;503 + knowledge_unavailable 给专门提示;
//   3. SSR 安全:初始 loading/error/result 均为空态,不渲染结果区,
//      表单本身可 SSR 直渲;提交与结果渲染发生在 hydration 之后。
// D6-T6:扩展为知识库管理页(检索区保留,新增管理区):
//   - 上传区:file input(accept .pdf/.txt 仅作前端提示,大小/类型仍由
//     服务端校验,超限 422 兜底)+ 上传按钮;上传中禁用;成功展示解析
//     回执(文档 id/来源/页数/分块数)并刷新列表;422 提示「文件类型
//     或大小不符」;
//   - 文档列表:挂载时 useEffect 拉取(setState 全部在 await 之后的
//     异步回调里,符合 react-hooks「effect 内同步 setState」规则——
//     官方数据拉取模式:effect 内局部 async 函数);删除走
//     window.confirm 确认(只在点击事件回调里执行,SSR 不经过该路径);
//     上传/删除成功后刷新列表;
//   - 知识条目浏览/编辑:本期降级为只展示文档元数据——后端注册表只
//     存元数据,无 chunk 浏览端点;条目级浏览/编辑依赖后端 chunk
//     端点,不在本期范围(见 D6-T6 完成备注)。
import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { DocumentTreeView, type TreeResponse } from "@/components/document-tree-view";
import { NamespaceSelector } from "@/components/namespace-selector";
import { Skeleton } from "@/components/ui/skeleton";
import type { components } from "@/contracts/api.generated";
import {
  ApiClientError,
  apiClient,
  type KnowledgeDocumentEntry,
  type KnowledgeDocumentUploadResponse,
  type KnowledgeSearchResult,
} from "@/lib/api-client";

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
  // —— 检索区状态(D6-T4) ——
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);

  // —— 管理区状态(D6-T6) ——
  // 文档列表 / 列表加载与错误 / 选中文件 / 上传态与回执 / 删除中的 id
  const [documents, setDocuments] = useState<KnowledgeDocumentEntry[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<KnowledgeDocumentUploadResponse | null>(
    null,
  );
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  // S5-C2：文档结构树缓存（首次展开时拉取）+ 当前展开的文档 id。
  const [trees, setTrees] = useState<Record<string, TreeResponse>>({});
  const [expandedDocId, setExpandedDocId] = useState<string | null>(null);
  // S5-C1：上传目标空间（默认 public，选择器含「＋ 新建空间」入口）。
  const [uploadNamespace, setUploadNamespace] = useState("public");

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

  // D6-T6:拉取文档清单——挂载、上传/删除成功后与手动刷新按钮复用。
  // setState 全部发生在 await 之后(异步回调),不触发 react-hooks 的
  // 「effect 内同步 setState」规则;useCallback([]) 保证 effect 依赖稳定。
  const refreshDocuments = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      const response = await apiClient.listDocuments();
      setDocuments(response.documents);
    } catch (caught) {
      setListError(errorText(caught));
    } finally {
      setListLoading(false);
    }
  }, []);

  // S5-C2:按需拉取文档结构树——首次展开时请求,结果缓存进 trees。
  const loadTree = useCallback(
    async (documentId: string) => {
      try {
        const tree = await apiClient.getDocumentTree(documentId);
        setTrees((prev) => ({ ...prev, [documentId]: tree }));
      } catch {
        setTrees((prev) => ({
          ...prev,
          [documentId]: {
            kind: "flat",
            document_id: documentId,
            flat_pages: [],
          },
        }));
      }
    },
    [],
  );

  // 按空间分组(从 document_id 前缀推导:非 public 前缀为空间名)。
  const groupedDocuments = (() => {
    const groups = new Map<string, KnowledgeDocumentEntry[]>();
    for (const doc of documents) {
      const colon = doc.document_id.indexOf(":");
      const ns = colon > 0 ? doc.document_id.slice(0, colon) : "public";
      const bucket = groups.get(ns) ?? [];
      bucket.push(doc);
      groups.set(ns, bucket);
    }
    // public 恒排首位,其余按空间名稳定排序。
    return [...groups.entries()].sort(([a], [b]) => {
      if (a === "public") {
        return -1;
      }
      if (b === "public") {
        return 1;
      }
      return a < b ? -1 : a > b ? 1 : 0;
    });
  })();

  // 挂载拉取:采用 React 官方数据拉取模式——effect 内局部 async 函数,
  // setState 在 await 之后的异步回调里执行(规则只拦 effect 同步体内
  // 的 setState),ignore 标志防止卸载后 setState。
  useEffect(() => {
    let ignore = false;
    async function load() {
      // review nit:listLoading 初始即 true,await 前无需再置位
      // (与「setState 全在 await 后」注释保持一致)。
      try {
        const response = await apiClient.listDocuments();
        if (ignore) {
          return;
        }
        setDocuments(response.documents);
      } catch (caught) {
        if (ignore) {
          return;
        }
        setListError(errorText(caught));
      } finally {
        if (!ignore) {
          setListLoading(false);
        }
      }
    }
    void load();
    return () => {
      ignore = true;
    };
  }, []);

  const handleUpload = async () => {
    if (!selectedFile || uploading) {
      return;
    }
    setUploading(true);
    setUploadError(null);
    setUploadResult(null);
    try {
      const uploaded = await apiClient.uploadDocument(
        selectedFile,
        undefined,
        uploadNamespace,
      );
      setUploadResult(uploaded);
      setSelectedFile(null);
      // 上传成功(后端幂等替换)后刷新列表,新文档立即可见
      await refreshDocuments();
    } catch (caught) {
      // 422(文件类型/大小不符)给专门文案,其余走 ApiClientError 归一
      setUploadError(
        caught instanceof ApiClientError && caught.status === 422
          ? "文件类型或大小不符,请上传 .pdf/.txt 且不超过服务端限制。"
          : errorText(caught),
      );
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (documentId: string) => {
    // review nit:防重入检查移到 confirm 之前,避免删 A 时点删 B 先
    // 弹框、确认后被静默拦截的无谓弹窗。
    if (deletingId !== null) {
      return;
    }
    // window.confirm 只在点击事件回调(客户端专属)里执行,SSR 不经过
    // 该路径,安全;用户取消则直接返回,不发请求。
    if (!window.confirm("确认删除该文档?删除后不可恢复。")) {
      return;
    }
    setDeletingId(documentId);
    try {
      await apiClient.deleteDocument(documentId);
      // 删除成功后刷新列表(与上传成功刷新同一路径)
      await refreshDocuments();
    } catch (caught) {
      // 删除失败:列表保持原状,错误行展示
      setListError(errorText(caught));
    } finally {
      setDeletingId(null);
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

      {/* D6-T6:知识库管理区——上传 + 文档列表(挂载拉取;上传/删除后刷新)。
          条目浏览/编辑依赖后端 chunk 端点,本期降级为只展示元数据。 */}
      <section
        aria-label="知识库管理"
        className="mt-6 rounded-lg border border-border bg-card p-5"
        data-slot="document-manager"
      >
        <h2 className="text-body font-semibold text-foreground">知识库管理</h2>

        {/* 上传区:accept 仅作前端提示,大小/类型仍由服务端校验(422 兜底) */}
        <div className="mt-4" data-slot="upload-area">
          <div className="mb-3 max-w-xs" data-slot="upload-namespace">
            <p className="mb-1 text-caption font-medium text-foreground">目标知识空间</p>
            <NamespaceSelector
              onChange={setUploadNamespace}
              value={uploadNamespace}
            />
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <input
              aria-label="选择要上传的知识文档"
              className="block w-full max-w-xs text-body text-foreground file:mr-3 file:rounded-md file:border file:border-border file:bg-muted file:px-3 file:py-1.5 file:text-caption file:font-medium file:text-foreground"
              data-slot="upload-input"
              type="file"
              accept=".pdf,.txt"
              onChange={(event) => setSelectedFile(event.target.files?.[0] ?? null)}
            />
            <Button
              data-slot="upload-btn"
              disabled={uploading || selectedFile === null}
              onClick={() => void handleUpload()}
              type="button"
            >
              {uploading ? "上传中…" : "上传"}
            </Button>
          </div>
          <p className="mt-2 text-caption text-muted-foreground">
            支持 .pdf/.txt;大小与内容类型由服务端校验,超限返回 422(前端 accept 仅作提示)。
          </p>
          {uploadError ? (
            <p
              className="mt-3 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-caption text-foreground"
              data-slot="upload-error"
              role="alert"
            >
              {uploadError}
            </p>
          ) : null}
          {uploadResult ? (
            <p
              className="mt-3 rounded-md border border-border bg-muted/50 px-3 py-2 text-caption text-foreground"
              data-slot="upload-result"
            >
              已上传:文档 {uploadResult.document_id} · 来源 {uploadResult.source} · 页数{" "}
              {uploadResult.page_count ?? "—"} · 分块 {uploadResult.chunk_count ?? "—"}
            </p>
          ) : null}
        </div>

        {/* 文档列表:加载中骨架 → 错误行 → 空态 → 条目列表 */}
        <div className="mt-4 border-t border-border pt-4" data-slot="document-list">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-caption font-medium text-foreground">已上传文档</h3>
            <Button
              data-slot="document-refresh"
              disabled={listLoading}
              onClick={() => void refreshDocuments()}
              size="sm"
              type="button"
              variant="outline"
            >
              刷新
            </Button>
          </div>
          {listLoading ? (
            <div
              aria-label="加载中"
              className="mt-3 space-y-2"
              data-slot="document-loading"
              role="status"
            >
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : listError ? (
            <p
              className="mt-3 flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-caption text-foreground"
              data-slot="document-list-error"
              role="alert"
            >
              {listError}
            </p>
          ) : documents.length === 0 ? (
            <p
              className="mt-3 rounded-md border border-dashed border-border px-3 py-6 text-center text-caption text-muted-foreground"
              data-slot="document-empty"
            >
              暂无上传文档
            </p>
          ) : (
            groupedDocuments.map(([namespace, docs]) => (
              <section data-slot="namespace-group" key={namespace}>
                <h4
                  className="mt-4 flex items-center gap-2 text-caption font-medium text-muted-foreground"
                  data-slot="namespace-group-title"
                >
                  <span className="rounded bg-muted px-1.5 py-0.5">{namespace}</span>
                  <span>{docs.length} 篇</span>
                </h4>
                <ul className="mt-2 space-y-2">
                  {docs.map((doc) => {
                    const expanded = expandedDocId === doc.document_id;
                    const tree = trees[doc.document_id];
                    return (
                      <li
                        className="rounded-md border border-border px-3 py-2"
                        data-slot="document-item"
                        key={doc.document_id}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="truncate text-body text-foreground">{doc.source}</p>
                            <p className="text-caption text-muted-foreground">
                              文档 {doc.document_id} · 页数 {doc.page_count ?? "—"} · 分块{" "}
                              {doc.chunk_count ?? "—"}
                            </p>
                          </div>
                          <div className="flex shrink-0 gap-2">
                            <Button
                              data-slot="document-tree-toggle"
                              onClick={() => {
                                if (expanded) {
                                  setExpandedDocId(null);
                                  return;
                                }
                                setExpandedDocId(doc.document_id);
                                if (trees[doc.document_id] === undefined) {
                                  void loadTree(doc.document_id);
                                }
                              }}
                              size="sm"
                              type="button"
                              variant="outline"
                            >
                              {expanded ? "收起结构" : "展开结构"}
                            </Button>
                            <Button
                              data-slot="document-delete"
                              disabled={deletingId === doc.document_id}
                              onClick={() => void handleDelete(doc.document_id)}
                              size="sm"
                              type="button"
                              variant="outline"
                            >
                              {deletingId === doc.document_id ? "删除中…" : "删除"}
                            </Button>
                          </div>
                        </div>
                        {expanded && tree !== undefined ? (
                          <div className="mt-2" data-slot="document-tree-panel">
                            <DocumentTreeView tree={tree} />
                          </div>
                        ) : null}
                        {expanded && tree === undefined ? (
                          <p className="mt-2 text-caption text-muted-foreground">结构加载中…</p>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))
          )}
        </div>
      </section>

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
