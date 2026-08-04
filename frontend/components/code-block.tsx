import { useState, type ReactNode } from "react";

// D3-T3:代码块复制按钮。
//
// 设计说明:
// - copyCode 抽成纯函数、clipboard 可注入:SSR / node:test 环境没有
//   navigator.clipboard,注入 mock 便于单测;成功返回 true,失败返回 false。
// - textFromChildren 递归提取 React 子树的纯文本:复制内容 = 高亮后
//   code 的纯文本,不含 HTML 标签(hljs 注入的 span 类名不进剪贴板)。
// - CodeBlock 不声明 "use client":它只被 assistant-markdown("use
//   client")引用,客户端边界由父组件承担,保持与 assistant-markdown
//   一致(避免重复指令造成歧义)。
//
// 已知取舍:组件卸载后 setTimeout 仍会触发 setCopied——React 18+ 对
// 已卸载组件的 setState 不再告警,1500ms 的复位定时器直接忽略即可。

export type ClipboardLike = Pick<Clipboard, "writeText">;

export async function copyCode(
  text: string,
  clipboard: ClipboardLike,
): Promise<boolean> {
  try {
    await clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// 递归提取 ReactNode 子树的纯文本:支持 string / number / 数组 /
// 元素(元素只取 props.children,忽略属性;portal 同理)。其余节点
// (布尔、null、无 props 的 iterable 等)贡献空串。
export function textFromChildren(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(textFromChildren).join("");
  }
  if (node && typeof node === "object" && "props" in node) {
    const props = (node as { props?: { children?: ReactNode } }).props;
    return textFromChildren(props?.children ?? null);
  }
  return "";
}

export function CodeBlock({
  children,
  text,
  clipboard,
}: {
  children: ReactNode;
  text: string;
  clipboard?: ClipboardLike;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    const ok = await copyCode(text, clipboard ?? navigator.clipboard);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <div className="relative" data-slot="code-block">
      <button
        className="absolute right-2 top-2 rounded border border-neutral-700 bg-neutral-800 px-2 py-0.5 text-caption text-neutral-300 hover:bg-neutral-700"
        data-slot="code-copy"
        onClick={() => void handleCopy()}
        type="button"
      >
        {copied ? "已复制" : "复制"}
      </button>
      {children}
    </div>
  );
}
