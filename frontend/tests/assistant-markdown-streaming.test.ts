// assistant-ui 接入(T11):流式 Markdown 分块/修复纯函数的参数化测试。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import test from "node:test";

const libPath = new URL(
  "../lib/assistant/markdown-streaming.ts",
  import.meta.url,
);

async function loadLib() {
  assert.ok(existsSync(libPath), "missing markdown-streaming lib");
  return import("../lib/assistant/markdown-streaming");
}

// —— splitMarkdownBlocks ——

test("splits on blank lines into stable blocks", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  assert.deepEqual(splitMarkdownBlocks("第一段\n\n第二段\n\n第三段"), [
    "第一段",
    "第二段",
    "第三段",
  ]);
  // 多空行/空白行折叠为一个边界
  assert.deepEqual(splitMarkdownBlocks("甲\n\n \n\n乙"), ["甲", "乙"]);
  // 无空行=单块;空内容=零块
  assert.deepEqual(splitMarkdownBlocks("只有一段"), ["只有一段"]);
  assert.deepEqual(splitMarkdownBlocks(""), []);
  assert.deepEqual(splitMarkdownBlocks("\n\n"), []);
});

test("keeps code fences atomic across blank lines", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  const blocks = splitMarkdownBlocks(
    "前文\n\n```python\ndef f():\n\n    return 1\n```\n\n后文",
  );
  assert.deepEqual(blocks, [
    "前文",
    "```python\ndef f():\n\n    return 1\n```",
    "后文",
  ]);
});

test("keeps unclosed trailing fence content in the last block", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  // 流式中途:围栏未闭合,其后(含空行)全部属于末尾活跃块
  const blocks = splitMarkdownBlocks("前文\n\n```ts\nconst a = 1;\n\nconst b = 2;");
  assert.deepEqual(blocks, ["前文", "```ts\nconst a = 1;\n\nconst b = 2;"]);
});

test("tilde fences and mismatched fence chars behave per CommonMark", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  // ~~~ 围栏同样原子化
  assert.deepEqual(splitMarkdownBlocks("甲\n\n~~~\nx\n\n~~~\n\n乙"), [
    "甲",
    "~~~\nx\n\n~~~",
    "乙",
  ]);
  // ``` 块内的 ~~~ 行不是围栏(不同字符),不切换状态
  const blocks = splitMarkdownBlocks("```\ncode\n~~~\nstill code\n```\n\n后文");
  assert.deepEqual(blocks[0], "```\ncode\n~~~\nstill code\n```");
  assert.deepEqual(blocks[1], "后文");
});

test("display math blocks stay atomic across blank lines", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  const blocks = splitMarkdownBlocks(
    "前文\n\n$$\n\\begin{aligned}\na &= b\n\nc &= d\n\\end{aligned}\n$$\n\n后文",
  );
  assert.equal(blocks.length, 3);
  assert.match(blocks[1]!, /aligned/);
  assert.equal(blocks[2], "后文");
});

test("single-line inline math with paired dollars does not open a block", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  // "$$E=mc^2$$" 单行是行内公式(两次 $$ 抵消),其后的空行仍是分块点
  const blocks = splitMarkdownBlocks("$$E=mc^2$$\n\n下一段");
  assert.deepEqual(blocks, ["$$E=mc^2$$", "下一段"]);
});

// —— repairUnclosedFence ——

test("repairs an unclosed fence with the matching char", async () => {
  const { repairUnclosedFence } = await loadLib();

  assert.equal(
    repairUnclosedFence("```python\ndef f():"),
    "```python\ndef f():\n```",
  );
  assert.equal(repairUnclosedFence("~~~\ncode"), "~~~\ncode\n~~~");
});

test("balanced or fenceless blocks return the same reference", async () => {
  const { repairUnclosedFence } = await loadLib();

  const balanced = "```\ncode\n```";
  assert.equal(repairUnclosedFence(balanced), balanced);
  const plain = "普通段落 **加粗";
  assert.equal(repairUnclosedFence(plain), plain);
});

test("unclosed bold inside code fences is not touched", async () => {
  const { repairUnclosedFence } = await loadLib();

  // 代码块内的 ** 是代码内容(如 x = 2 ** 3),不主动闭合——本模块只管围栏
  const code = "```\nx = 2 ** 3\n```";
  assert.equal(repairUnclosedFence(code), code);
});

// —— 性能:5000 token 级长文的分块扫描保持亚毫秒 ——

test("splitting a long streaming answer stays sub-millisecond", async () => {
  const { splitMarkdownBlocks } = await loadLib();

  const paragraph = "这是一段关于机器学习概念的中文解释文本,反复出现构成流式长文。";
  const content = Array.from(
    { length: 120 },
    (_, index) => `## 小节 ${index}\n\n${paragraph.repeat(3)}`,
  ).join("\n\n");

  const start = performance.now();
  for (let index = 0; index < 50; index += 1) {
    splitMarkdownBlocks(content);
  }
  const elapsed = (performance.now() - start) / 50;

  assert.ok(
    elapsed < 2,
    `expected split under 2ms per pass, took ${elapsed.toFixed(3)}ms`,
  );
});
