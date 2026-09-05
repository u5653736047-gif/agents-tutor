import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

const componentPath = new URL("../components/grading-card.tsx", import.meta.url);

async function loadGradingCard() {
  assert.ok(existsSync(componentPath), "missing grading card component");
  return import("../components/grading-card");
}

// P2-12（审查 W4）：批改结果卡片渲染测试。
// 与后端契约 GradingResultDto 同构：逐题明细（知识点/错因/反馈）、
// 总分概览、总评与「建议评分，教师复核」产品口径标注。
const grading = {
  items: [
    {
      question_id: "q1",
      score: 10,
      max_score: 10,
      feedback: "解答完整。",
      knowledge_point: "梯度下降",
      error_tag: null,
    },
    {
      question_id: "q2",
      score: 5,
      max_score: 10,
      feedback: "概念错误，建议复习。",
      knowledge_point: null,
      error_tag: "概念不清",
    },
  ],
  overall_comment: "整体掌握一般。",
  total_score: 15,
  max_total_score: 20,
};

test("the grading card renders total score, per-item details and disclaimer", async () => {
  const { GradingCard } = await loadGradingCard();

  assert.equal(typeof GradingCard, "function", "missing grading card renderer");
  const markup = renderToStaticMarkup(createElement(GradingCard, { grading }));

  // 卡片容器 + 总分概览 + 两条逐题明细
  assert.match(markup, /data-slot="grading-card"/);
  assert.match(markup, /data-slot="grading-total"/);
  assert.equal(markup.match(/data-slot="grading-item"/g)?.length, 2);
  assert.match(markup, /15 \/ 20 分/);
  // 逐题字段：知识点/未分类、得分、错因、反馈
  assert.match(markup, /梯度下降/);
  assert.match(markup, /未分类知识点/);
  assert.match(markup, /10 \/ 10/);
  assert.match(markup, /5 \/ 10/);
  assert.match(markup, /data-slot="grading-error-tag"/);
  assert.match(markup, /概念不清/);
  assert.match(markup, /data-slot="grading-feedback"/);
  // 总评 + 产品口径标注（LLM 主观题评分是建议性质）
  assert.match(markup, /data-slot="grading-comment"/);
  assert.match(markup, /整体掌握一般。/);
  assert.match(markup, /data-slot="grading-disclaimer"/);
  assert.match(markup, /建议评分，教师复核/);
});

test("the grading card renders nothing for null grading", async () => {
  const { GradingCard } = await loadGradingCard();

  // 降级红线：非批改轮 grading 为 null，零渲染（不占位、不报错）
  const markup = renderToStaticMarkup(createElement(GradingCard, { grading: null }));

  assert.equal(markup, "");
});
