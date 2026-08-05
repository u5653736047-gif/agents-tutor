// shadcn 风格骨架屏:animate-pulse 呼吸 + 占位底色(语义 token bg-muted,
// 亮/暗自动适配,D5-T1 审计口径)。仅作加载占位,不承载语义内容;
// 具体形状(圆/条/块)由调用方通过 className 拼装。
import { cn } from "@/lib/utils";

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} />;
}
