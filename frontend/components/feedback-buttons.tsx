"use client";

import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";

// D6-T2:消息反馈交互(点赞/点踩 + 点踩纠错文本)。纯展示组件:props
// 只带会话/消息标识与提交回调,交互状态全部是组件本地 useState——
// SSR 初始渲染为「未选态」,刷新页面后组件重挂载回到未选态,可重新
// 评分(本地记忆:刻意不做 localStorage 持久化,任务「刷新可再评」)。
//
// 交互状态机:
//   idle(未选) --点 up--> 立即提交 onFeedback("up") --成功--> done(置灰)
//                                             └--失败--> idle + 错误行(可重试)
//   idle --点 down--> down 选中 + 展开纠错区 --再点 down--> 取消回 idle
//   down 选中 --提交纠错--> onFeedback("down", 文本) --成功--> done(清空文本+置灰)
//                                             └--失败--> 取消选中收起纠错 + 错误行
//                                                       (comment 保留,可重试)
//   done:全部控件 disabled(选中态保留);失败不阻塞对话,错误只显示在
//   组件内错误行,不进入全局 requestError。
type FeedbackRating = "up" | "down";

type FeedbackButtonsProps = {
  // 会话与消息标识:由调用方(MessageRow 所在会话)注入,供提交时
  // 定位上下文;实际请求由调用方闭包(onFeedback)组装,组件本身
  // 不直接访问它们。
  sessionId: string;
  messageId?: string;
  onFeedback: (rating: FeedbackRating, comment?: string) => Promise<void> | void;
};

// 图标按钮基础样式:小方钮、hover 底色、disabled 半透明
const iconButtonClass =
  "inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-50";

export function FeedbackButtons({ onFeedback }: FeedbackButtonsProps) {
  const [rating, setRating] = useState<FeedbackRating | null>(null);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 提交中或已成功:所有控件禁用
  const locked = submitting || done;

  // 统一提交收尾:成功置灰(done);失败复位交互态(rating 取消选中、
  // 收起纠错区)并显示错误行,comment 保留供重试——「状态复位 +
  // 允许重试」。onFeedback 失败(拒绝/抛错)在这里捕获,绝不外抛。
  const submit = async (target: FeedbackRating, text?: string) => {
    if (locked) return;
    setSubmitting(true);
    setError(null);
    try {
      await onFeedback(target, text);
      setDone(true);
      if (target === "down") {
        setComment("");
      }
    } catch (cause) {
      setRating(null);
      setCorrectionOpen(false);
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "反馈提交失败，请稍后重试。",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const handleRateUp = () => {
    if (locked) return;
    // 再点同项取消(可再评):仅发生在未提交成功前(如上次失败后
    // 已复位;成功后按钮 disabled,刷新页面方可再评)
    if (rating === "up") {
      setRating(null);
      setCorrectionOpen(false);
      setError(null);
      return;
    }
    setRating("up");
    setCorrectionOpen(false);
    setError(null);
    void submit("up");
  };

  const handleRateDown = () => {
    if (locked) return;
    // 再点同项取消:收起纠错区、复位选中(尚未提交)
    if (rating === "down") {
      setRating(null);
      setCorrectionOpen(false);
      setError(null);
      return;
    }
    setRating("down");
    setCorrectionOpen(true);
    setError(null);
  };

  const handleSubmitCorrection = () => {
    if (locked) return;
    // 空文本也允许提交(仅点踩语义,规格);comment 原样传给回调
    void submit("down", comment);
  };

  return (
    <div className="mt-2">
      <div className="flex items-center gap-1">
        <button
          aria-label="点赞"
          aria-pressed={rating === "up"}
          className={
            iconButtonClass +
            (rating === "up" ? " text-primary" : "")
          }
          data-slot="feedback-up"
          disabled={locked}
          onClick={handleRateUp}
          type="button"
        >
          <ThumbsUp className="size-4" />
        </button>
        <button
          aria-label="点踩"
          aria-pressed={rating === "down"}
          className={
            iconButtonClass +
            (rating === "down" ? " text-primary" : "")
          }
          data-slot="feedback-down"
          disabled={locked}
          onClick={handleRateDown}
          type="button"
        >
          <ThumbsDown className="size-4" />
        </button>
      </div>

      {/* 失败错误行:独立于纠错区之外,点赞失败同样可见;只影响本组件 */}
      {error ? (
        <p
          className="mt-1 text-caption text-destructive"
          data-slot="feedback-error"
          role="alert"
        >
          {error}
        </p>
      ) : null}

      {/* 点踩后展开的纠错区:文本域(可选)+ 提交按钮 */}
      {correctionOpen ? (
        <div className="mt-2 space-y-2">
          <textarea
            className="w-full resize-none rounded-md border border-border bg-card px-3 py-2 text-caption text-foreground placeholder:text-muted-foreground disabled:pointer-events-none disabled:opacity-50"
            data-slot="feedback-correction"
            disabled={locked}
            onChange={(event) => setComment(event.target.value)}
            placeholder="补充说明(可选),帮助改进回答"
            rows={2}
            value={comment}
          />
          <Button
            data-slot="feedback-submit-correction"
            disabled={locked}
            onClick={handleSubmitCorrection}
            size="sm"
            type="button"
            variant="outline"
          >
            {submitting ? "提交中…" : "提交纠错"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
