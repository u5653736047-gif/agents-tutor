// S5-B4 生成物预览测试：入口显隐白名单、HTML 消毒安全边界、xlsx 解析。
//
// 消毒测试先挂 jsdom window 再动态 import（lib/sanitize-html.ts 惰性
// 绑定 DOMPurify 到 globalThis.window，见该模块注释）。
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const previewModuleUrl = new URL(
  "../components/generated-file-preview.tsx",
  import.meta.url,
);

test("canPreview whitelists docx/xlsx and excludes pptx and others", async () => {
  const { canPreview } = await import(previewModuleUrl);

  assert.equal(canPreview("教案.docx"), true);
  assert.equal(canPreview("成绩.xlsx"), true);
  assert.equal(canPreview("课件.pptx"), false, "pptx 不支持预览");
  assert.equal(canPreview("说明.pdf"), false);
  assert.equal(canPreview("README.md"), false);
  assert.equal(canPreview("无扩展名"), false);
  // 大小写不敏感。
  assert.equal(canPreview("报告.DOCX"), true);
});

test("preview toggle renders for xlsx and not for pptx", async () => {
  const { GeneratedFilePreview } = await import(previewModuleUrl);

  const xlsxMarkup = renderToStaticMarkup(
    createElement(GeneratedFilePreview, {
      fileId: "f-1",
      name: "成绩.xlsx",
    }),
  );
  assert.match(xlsxMarkup, /data-slot="generated-file-preview"/);
  assert.match(xlsxMarkup, /预览/);

  const pptxMarkup = renderToStaticMarkup(
    createElement(GeneratedFilePreview, {
      fileId: "f-2",
      name: "课件.pptx",
    }),
  );
  assert.equal(pptxMarkup, "", "pptx 不渲染任何预览入口");
});

test("sanitizeGeneratedHtml strips scripts and event handlers", async () => {
  // 先挂 DOM 再导入：DOMPurify 需要真实 DOM。
  const { JSDOM } = await import("jsdom");
  (globalThis as { window?: unknown }).window = new JSDOM("").window;
  const { sanitizeGeneratedHtml } = await import("../lib/sanitize-html");

  const malicious =
    '<p onclick="steal()">正常<b>内容</b></p><script>alert(1)</script>' +
    '<img src=x onerror="alert(2)"><iframe src="https://evil.example"></iframe>';
  const clean = sanitizeGeneratedHtml(malicious);

  assert.match(clean, /正常/);
  assert.match(clean, /<b>内容<\/b>/);
  assert.doesNotMatch(clean, /<script/i);
  assert.doesNotMatch(clean, /onerror/i);
  assert.doesNotMatch(clean, /onclick/i);
  assert.doesNotMatch(clean, /<iframe/i);
});

test("parseXlsxToHtml renders a real workbook as a table", async () => {
  // 用 SheetJS 自身生成最小工作簿作为夹具（写与读同一实现，往返自洽）。
  const XLSX = await import("xlsx");
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(
    workbook,
    XLSX.utils.aoa_to_sheet([
      ["姓名", "得分"],
      ["小明", 95],
    ]),
    "Sheet1",
  );
  const bytes: ArrayBuffer = XLSX.write(workbook, {
    bookType: "xlsx",
    type: "array",
  });

  const previewModule = await import(previewModuleUrl);
  const html = await previewModule.parseXlsxToHtml(new Blob([bytes]));

  assert.match(html, /<table/i);
  assert.match(html, /姓名/);
  assert.match(html, /95/);
});

test("parseDocxToHtml rejects non-docx payloads (error state semantics)", async () => {
  const previewModule = await import(previewModuleUrl);

  // mammoth 要求 docx（zip 容器）：垃圾字节必然解析失败，组件据此
  // 进入 error 态（见 generated-file-preview.tsx 的 try/catch）。
  await assert.rejects(
    () => previewModule.parseDocxToHtml(new Blob([new Uint8Array([1, 2, 3])])),
  );
});

test("MAX_PREVIEW_BYTES guard contract", async () => {
  const { MAX_PREVIEW_BYTES } = await import(previewModuleUrl);

  // 20MB 护栏：廉价防御而非常规路径（生成文件通常远小于该值）。
  assert.equal(MAX_PREVIEW_BYTES, 20 * 1024 * 1024);
});
