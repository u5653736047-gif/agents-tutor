// AI 生成内容标识（伦理合规硬性要求：参赛材料《02—伦理与安全合规性
// 声明》承诺「在作品系统中设置明显的『AI 生成内容』标识」）。
//
// 纯展示组件、零状态，两种形态：
//   - message：助手消息气泡内的常驻标识（每条助手回答都携带，最显眼）；
//   - footer：独立页面（学习进度/知识库）底部的全局声明。
// 文案克制统一：只声明生成性质与复核义务，不做额外解释。

export type AiContentNoticeProps = {
  variant?: "message" | "footer";
};

export function AiContentNotice({ variant = "message" }: AiContentNoticeProps) {
  if (variant === "footer") {
    return (
      <p
        className="mt-10 pb-4 text-center text-caption text-muted-foreground"
        data-slot="ai-content-notice"
      >
        本系统内容由人工智能生成或聚合，仅供参考，重要信息请人工复核。
      </p>
    );
  }
  return (
    <p
      className="mt-2 text-caption text-muted-foreground/80"
      data-slot="ai-content-notice"
    >
      内容由 AI 生成，仅供参考，重要信息请人工复核
    </p>
  );
}
