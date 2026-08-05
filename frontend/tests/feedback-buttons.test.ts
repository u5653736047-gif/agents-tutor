import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const componentPath = new URL("../components/feedback-buttons.tsx", import.meta.url);

async function loadFeedbackButtons() {
  assert.ok(existsSync(componentPath), "missing feedback buttons");
  return import("../components/feedback-buttons");
}

// D6-T2:回答反馈交互 ———————————————————————————————————————————————
test("feedback buttons render an idle SSR state without correction area or errors", async () => {
  const { FeedbackButtons } = await loadFeedbackButtons();

  const markup = renderToStaticMarkup(
    createElement(FeedbackButtons, {
      onFeedback: async () => undefined,
      sessionId: "session-1",
    }),
  );

  // 点赞/点踩两个图标按钮
  assert.match(markup, /data-slot="feedback-up"/);
  assert.match(markup, /data-slot="feedback-down"/);
  // 初始未选态:两个按钮都未按下
  assert.equal((markup.match(/aria-pressed="false"/g) ?? []).length, 2);
  // 未点踩:不渲染纠错文本域、提交按钮与错误行
  assert.doesNotMatch(markup, /data-slot="feedback-correction"/);
  assert.doesNotMatch(markup, /data-slot="feedback-submit-correction"/);
  assert.doesNotMatch(markup, /data-slot="feedback-error"/);
});

test("feedback buttons implement the interaction state machine locally", () => {
  const source = readFileSync(componentPath, "utf8");

  // 点赞即时提交;点踩展开纠错区,提交走 onFeedback("down", 文本)
  assert.match(source, /void submit\("up"\)/);
  assert.match(source, /data-slot="feedback-correction"/);
  assert.match(source, /data-slot="feedback-submit-correction"/);
  assert.match(source, /void submit\("down", comment\)/);
  assert.match(source, /await onFeedback\(target, text\)/);
  // 再点同项取消(可再评)
  assert.match(source, /setRating\(null\)/);
  // 成功置灰(disabled)并清空纠错文本
  assert.match(source, /setDone\(true\)/);
  assert.match(source, /setComment\(""\)/);
  // 失败显示组件内错误行并允许重试(状态复位,不写全局状态)
  assert.match(source, /data-slot="feedback-error"/);
  assert.match(source, /setError\(/);
  // 本地记忆:刻意不做 localStorage 持久化——刷新后组件重挂载回到
  // 未选态,可重新评分(任务「刷新可再评」)。组件源码只在注释中提到
  // localStorage(说明意图),不得实际调用
  assert.doesNotMatch(source, /localStorage\.(get|set|remove)Item/);
  assert.doesNotMatch(source, /window\.localStorage/);
});
