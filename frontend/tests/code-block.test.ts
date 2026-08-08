import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const codeBlockPath = new URL("../components/code-block.tsx", import.meta.url);

async function loadCodeBlock() {
  assert.ok(existsSync(codeBlockPath), "missing safe code block component");
  return import("../components/code-block");
}

test("copyCode writes text to the injected clipboard and resolves true", async () => {
  const { copyCode } = await loadCodeBlock();
  const written: string[] = [];
  const clipboard = {
    writeText: async (text: string) => {
      written.push(text);
    },
  };

  const ok = await copyCode("const value = 1;", clipboard);

  assert.equal(ok, true);
  assert.deepEqual(written, ["const value = 1;"]);
});

test("copyCode resolves false when the clipboard write throws", async () => {
  const { copyCode } = await loadCodeBlock();
  const clipboard = {
    writeText: async () => {
      throw new Error("clipboard denied");
    },
  };

  const ok = await copyCode("const value = 1;", clipboard);

  assert.equal(ok, false);
});

test("CodeBlock renders the copy button with its children intact", async () => {
  const { CodeBlock } = await loadCodeBlock();
  const markup = renderToStaticMarkup(
    createElement(
      CodeBlock,
      { text: "const value = 1;" },
      createElement("pre", null, "const value = 1;"),
    ),
  );

  assert.match(markup, /data-slot="code-block"/);
  assert.match(markup, /data-slot="code-copy"/);
  assert.match(markup, />复制</);
  assert.match(markup, /<pre>const value = 1;<\/pre>/);
  // 「已复制」是点击后的交互状态,SSR(renderToStaticMarkup)无法触发
  // ——由 copyCode 成功路径单测 + 浏览器手动验收覆盖。
});

test("textFromChildren extracts plain text from a nested code element", async () => {
  const { textFromChildren } = await loadCodeBlock();
  const node = createElement("code", null, "def add():");
  assert.equal(textFromChildren(node), "def add():");
});

test("textFromChildren joins arrays and skips non-text nodes", async () => {
  const { textFromChildren } = await loadCodeBlock();
  // 模拟 rehype-highlight 高亮后的 code 子树:关键字/函数名被 span
  // 包裹,空白与标点是裸文本——提取结果应为不含任何标签的纯文本。
  const node = createElement(
    "code",
    null,
    createElement("span", { className: "hljs-keyword" }, "def"),
    " ",
    createElement("span", { className: "hljs-title" }, "add"),
    "():",
    null,
    false,
  );

  assert.equal(textFromChildren(node), "def add():");
});
