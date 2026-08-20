"use client";

// P2-12:批改结果卡片(仿 citation-list 的纯展示组件契约)。
// 展示一次作业批改的结构化结论(ChatResponse.grading / StreamEvent
// grading / 历史消息 Message.grading,经 props 传入),自身不订阅 store,
// 便于 SSR 渲染与组件测试:
//   1. 降级红线:grading 为 null 时零渲染(return null)——非批改轮次、
//      历史轮次无批改时必须静默降级,不占位、不报错;
//   2. 总分概览 + 逐题明细(得分/满分、知识点、错因、反馈);
//   3. 产品口径(计划 P2-12 / 契约 GradingResultDto 注释):LLM 主观题
//      评分是建议性质,卡片必须标注「建议评分,教师复核」。
import type { components } from "@/contracts/api.generated";

type GradingResult = components["schemas"]["GradingResultDto"];

export type GradingCardProps = {
  grading: GradingResult | null;
};

function formatScore(value: number): string {
  // 满分/得分可能是小数(如 7.5),整数时去掉小数点保持简洁
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

export function GradingCard({ grading }: GradingCardProps) {
  // 降级红线:null(后端未携带批改)零渲染——批改是可选元数据,缺失时
  // 必须静默降级,不得渲染占位提示或报错(与 CitationList 同一契约)。
  if (grading == null) {
    return null;
  }

  return (
    <section
      aria-label="批改结果"
      className="rounded-lg border border-border bg-muted/50 px-4 py-3 text-caption"
      data-slot="grading-card"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-medium text-muted-foreground">批改结果</h3>
        <span className="font-medium text-foreground" data-slot="grading-total">
          {formatScore(grading.total_score)} / {formatScore(grading.max_total_score)} 分
        </span>
      </div>
      <ol className="mt-2 flex flex-col gap-2">
        {grading.items.map((item, index) => (
          <li
            className="rounded-md border border-border bg-card px-3 py-2"
            data-slot="grading-item"
            key={`${item.question_id}-${index}`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="min-w-0 truncate text-foreground">
                <span className="mr-1.5 text-muted-foreground">
                  第 {index + 1} 题
                </span>
                {item.knowledge_point ?? "未分类知识点"}
              </span>
              <span className="shrink-0 font-medium text-foreground">
                {formatScore(item.score)} / {formatScore(item.max_score)}
              </span>
            </div>
            <div className="mt-1 flex flex-col gap-0.5 text-muted-foreground">
              {item.error_tag ? (
                <span data-slot="grading-error-tag">错因:{item.error_tag}</span>
              ) : null}
              {item.feedback ? (
                <span data-slot="grading-feedback">{item.feedback}</span>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      {grading.overall_comment ? (
        <p className="mt-2 text-muted-foreground" data-slot="grading-comment">
          总评:{grading.overall_comment}
        </p>
      ) : null}
      {/* 产品口径：LLM 主观题评分是建议性质，必须标注复核提示 */}
      <p className="mt-2 text-muted-foreground/80" data-slot="grading-disclaimer">
        建议评分，教师复核
      </p>
    </section>
  );
}
