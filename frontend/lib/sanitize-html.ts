// S5-B4：生成物预览的 HTML 消毒（安全边界，见 generated-file-preview.tsx）。
//
// 为什么必须消毒：mammoth 把 docx 转出的 HTML、SheetJS 把 xlsx 转出的
// 表格 HTML 都源自「生成文件的内容」，而生成文件的内容最终来自模型
// 输出——不可信。未经消毒直插 DOM（dangerouslySetInnerHTML）构成
// 存储型 XSS（恶意 docx 内嵌 <script> 或 onerror 属性即可命中）。
// 因此渲染前必须经 DOMPurify 白名单消毒，这一步不可省略、不可绕过。
//
// 实现说明：DOMPurify 需要一个 DOM；浏览器端用全局 window，测试端由
// jsdom 提供（tests/generated-file-preview.test.ts 先挂 window 再动态
// import 本模块）。惰性创建单例，避免模块加载期触碰 DOM（SSR 安全）。
import createDOMPurify from "dompurify";

let cached: ReturnType<typeof createDOMPurify> | null = null;

function purifier(): ReturnType<typeof createDOMPurify> {
  if (cached === null) {
    const domWindow = (globalThis as { window?: Window }).window;
    if (domWindow === undefined) {
      throw new Error("sanitizeGeneratedHtml requires a DOM (browser or jsdom)");
    }
    // lib.dom 的 Window 类型与 DOMPurify 的 WindowLike（Pick<globalThis,
    // ...>）结构不重合，但运行时（浏览器全局 / jsdom window）均满足——
    // 经参数类型双向断言对齐，行为不变。
    const windowLike = domWindow as unknown as Parameters<
      typeof createDOMPurify
    >[0];
    cached = createDOMPurify(windowLike);
  }
  return cached;
}

export function sanitizeGeneratedHtml(html: string): string {
  // USE_PROFILES html：只保留常规 HTML 标签/属性白名单——script/style/
  // iframe/on* 事件属性一律剔除。
  return purifier().sanitize(html, { USE_PROFILES: { html: true } });
}
