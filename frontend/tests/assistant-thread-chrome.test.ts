// assistant-ui 接入(T10):Thread 周边件(骨架/错误块/状态播报)测试。
// 策略与 assistant-approval-cards.test.ts 一致:纯展示组件(StreamingEmpty)
// 走 renderToStaticMarkup 行为断言;store 连接组件(RunErrorBlocks/
// LiveStatusLine)在 SSR 下只见初始态(零渲染),wiring 走源码正则。
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const streamingEmptyPath = new URL(
  "../components/assistant-ui/parts/streaming-empty.tsx",
  import.meta.url,
);
const runErrorBlocksPath = new URL(
  "../components/assistant-ui/run-error-blocks.tsx",
  import.meta.url,
);
const threadPath = new URL(
  "../components/assistant-ui/assistant-thread.tsx",
  import.meta.url,
);

async function loadStreamingEmpty() {
  assert.ok(existsSync(streamingEmptyPath), "missing streaming empty part");
  return import("../components/assistant-ui/parts/streaming-empty");
}

async function loadRunErrorBlocks() {
  assert.ok(existsSync(runErrorBlocksPath), "missing run error blocks");
  return import("../components/assistant-ui/run-error-blocks");
}

// —— StreamingEmpty(纯展示,行为断言) ——

test("streaming empty part shows the skeleton only while running", async () => {
  const { StreamingEmpty } = await loadStreamingEmpty();

  const running = renderToStaticMarkup(
    createElement(StreamingEmpty, { status: { type: "running" } }),
  );
  assert.match(running, /data-slot="streaming-skeleton"/);
  // 骨架对读屏隐藏(进行中状态由 live-status 播报,不重复朗读)
  assert.match(running, /aria-hidden/);

  const settled = renderToStaticMarkup(
    createElement(StreamingEmpty, { status: { type: "complete" } }),
  );
  assert.equal(settled, "");
});

// —— RunErrorBlocks(store 连接,SSR 初始态 + 源码 wiring) ——

test("run error blocks render nothing in the initial store state", async () => {
  const { RunErrorBlocks } = await loadRunErrorBlocks();

  const markup = renderToStaticMarkup(createElement(RunErrorBlocks));
  assert.doesNotMatch(markup, /data-slot="run-error"/);
  assert.doesNotMatch(markup, /data-slot="request-error-network"/);
});

test("run error blocks wire store fields, presets and retry semantics", () => {
  const source = readFileSync(runErrorBlocksPath, "utf8");

  for (const field of [
    "runError",
    "requestError",
    "lastSentMessage",
    "retryLastMessage",
  ]) {
    assert.match(source, new RegExp(`state\\.${field}`), `missing ${field}`);
  }
  // 错误预设与锚点逐项对齐旧面板
  assert.match(source, /errorMessageFor\(runError\.error_code\)/);
  assert.match(source, /errorMessageFor\(null\)/);
  assert.match(source, /requestError\?\.code === null/);
  assert.match(source, /data-slot="run-error"/);
  assert.match(source, /data-slot="run-error-retry"/);
  assert.match(source, /data-slot="request-error-network"/);
  assert.match(source, /data-slot="request-error-retry"/);
  // 「有上一条消息才给重试入口」守卫
  assert.match(source, /lastSentMessage \?/);
});

// —— LiveStatusLine 与挂载位置(源码 wiring) ——

test("the thread virtualizes long conversations above the legacy threshold", () => {
  const source = readFileSync(threadPath, "utf8");
  const virtualizedPath = new URL(
    "../components/assistant-ui/virtualized-messages.tsx",
    import.meta.url,
  );
  const virtualizedSource = readFileSync(virtualizedPath, "utf8");

  // 阈值与旧路径同一边界(>50 条),参数照搬现版虚拟化配置
  assert.match(source, /messageIds\.length > 50/);
  assert.match(virtualizedSource, /estimateSize: \(\) => 96/);
  assert.match(virtualizedSource, /overscan: 8/);
  assert.match(virtualizedSource, /gap: 16/);
  // headless 组合:id 序列 + 按 id 渲染单条
  assert.match(virtualizedSource, /unstable_useThreadMessageIds/);
  assert.match(virtualizedSource, /Unstable_MessageById/);
  assert.match(virtualizedSource, /measureElement/);
});

test("the thread announces streaming state inside the aria-live region", () => {
  const source = readFileSync(threadPath, "utf8");

  // 播报行位于 aria-live 的 Viewport 内;订阅两个发送态并互斥取舍
  assert.match(source, /aria-live="polite"/);
  assert.match(source, /state\.isStreaming/);
  assert.match(source, /state\.isSending/);
  assert.match(source, /data-slot="live-status"/);
  assert.match(source, /助手正在生成回答…/);
  assert.match(source, /正在发送…/);
  // 挂载顺序:播报行 → 消息区(T13 起经 MessagesArea 全量/虚拟化分流)
  // → 审批卡片 → 错误块(带尖括号检索 JSX 用法,避开注释里的同名提及)
  const liveAt = source.indexOf("<LiveStatusLine");
  const messagesAt = source.indexOf("<MessagesArea");
  const cardsAt = source.indexOf("<ApprovalCards");
  const errorsAt = source.indexOf("<RunErrorBlocks");
  assert.ok(liveAt > 0 && liveAt < messagesAt, "live status before messages");
  assert.ok(messagesAt < cardsAt && cardsAt < errorsAt, "chrome order");
});
