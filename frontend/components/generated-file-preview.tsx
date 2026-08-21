"use client";

// S5-B4：生成物轻量在线预览（docx / xlsx 只读；pptx 不支持）。
//
// 范围边界（任务清单 B4）：只读预览，不编辑；后端零改动——复用
// AttachmentPreview 的受控下载通道（fetch + X-User-Id 鉴权头拉 Blob）。
//
// 安全要求（必做，见 lib/sanitize-html.ts 注释）：mammoth / SheetJS
// 产出的 HTML 源自模型生成的文件内容，不可信——渲染前必须经
// DOMPurify 白名单消毒。SheetJS 固定 0.20.3+（官方 CDN 分发，
// 已含 CVE-2023-30533 / CVE-2024-22363 修复；npm 源的 0.18.5 有
// 未修复通告，禁止降级回 npm 源版本）。
//
// 依赖加载时机：mammoth / xlsx 在点击「预览」时才动态 import——
// 拆进独立 chunk，不进首屏 bundle，未点预览的用户零成本；dompurify
// 是静态 import（见 lib/sanitize-html.ts），随面板 chunk 加载
// （约 20KB gz）——安全边界必须常驻，不做懒加载。
import { useState } from "react";

import { DEMO_USER_ID, getFileUrl } from "@/lib/api-client";
import { sanitizeGeneratedHtml } from "@/lib/sanitize-html";

// 支持预览的扩展名白名单；pptx 明确不支持（UI 不显示预览入口）。
const PREVIEWABLE_EXTENSIONS = new Set([".docx", ".xlsx"]);

export function canPreview(name: string): boolean {
  const dot = name.lastIndexOf(".");
  if (dot === -1) {
    return false;
  }
  return PREVIEWABLE_EXTENSIONS.has(name.slice(dot).toLowerCase());
}

export async function parseDocxToHtml(blob: Blob): Promise<string> {
  const mammoth = await import("mammoth");
  const result = await mammoth.convertToHtml({
    arrayBuffer: await blob.arrayBuffer(),
  });
  return result.value;
}

export async function parseXlsxToHtml(blob: Blob): Promise<string> {
  const XLSX = await import("xlsx");
  const workbook = XLSX.read(await blob.arrayBuffer(), { type: "array" });
  const firstSheetName = workbook.SheetNames[0];
  if (firstSheetName === undefined) {
    throw new Error("workbook has no sheets");
  }
  const sheet = workbook.Sheets[firstSheetName];
  if (sheet === undefined) {
    throw new Error("workbook sheet is missing");
  }
  // header/footer 置空：只保留 <table> 本体，外层 html/body 壳不需要。
  return XLSX.utils.sheet_to_html(sheet, { header: "", footer: "" });
}

type PreviewState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; html: string }
  | { kind: "error" };

export function GeneratedFilePreview({
  fileId,
  name,
}: {
  fileId: string;
  name: string;
}) {
  const [state, setState] = useState<PreviewState>({ kind: "idle" });

  if (!canPreview(name)) {
    // pptx / 其它类型：不渲染预览入口（保持下载，见模块注释）。
    return null;
  }

  async function toggle() {
    // 重入防护：loading 态重复点击会并发发起第二次 fetch+解析，
    // 后到者覆盖前者的 setState（浪费请求且大文件双倍解析开销）。
    if (state.kind === "loading") {
      return;
    }
    if (state.kind === "ready") {
      setState({ kind: "idle" });
      return;
    }
    setState({ kind: "loading" });
    try {
      const response = await fetch(getFileUrl(fileId), {
        headers: { "X-User-Id": DEMO_USER_ID },
      });
      if (!response.ok) {
        throw new Error(`file fetch failed: ${response.status}`);
      }
      const blob = await response.blob();
      const rawHtml = name.toLowerCase().endsWith(".docx")
        ? await parseDocxToHtml(blob)
        : await parseXlsxToHtml(blob);
      // 安全边界：消毒必须在 setState 之前（见 lib/sanitize-html.ts）。
      setState({ kind: "ready", html: sanitizeGeneratedHtml(rawHtml) });
    } catch {
      setState({ kind: "error" });
    }
  }

  return (
    <div data-slot="generated-file-preview">
      <button
        className="text-caption text-primary underline underline-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
        data-slot="generated-file-preview-toggle"
        disabled={state.kind === "loading"}
        onClick={() => {
          void toggle();
        }}
        type="button"
      >
        {state.kind === "ready" ? "收起预览" : "预览"}
      </button>
      {state.kind === "loading" ? (
        <p className="text-caption text-muted-foreground">预览生成中…</p>
      ) : null}
      {state.kind === "error" ? (
        <p className="text-caption text-destructive">
          预览失败（文件可能损坏或格式异常）
        </p>
      ) : null}
      {state.kind === "ready" ? (
        <div
          className="max-h-80 max-w-full overflow-auto rounded-md border border-border p-2 text-sm"
          data-slot="generated-file-preview-content"
          // 消毒后内容：见 lib/sanitize-html.ts 的安全边界说明。
          dangerouslySetInnerHTML={{ __html: state.html }}
        />
      ) : null}
    </div>
  );
}
