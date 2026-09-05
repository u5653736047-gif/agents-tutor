// assistant-ui 接入(T6):工具活动中文名映射。
// 从 collaboration-panel.tsx L69-83 受控复制——旧文件保持零改动(封存
// 基线),接受这一处受控重复换取旧路径零回归;两处映射必须保持一致,
// 新增工具时两边同步(旧路径下线后本文件成为唯一实现,删除旧表)。

/** 已知工具的中文活动名;未登记工具原样展示 tool_name(诚实降级) */
export const toolActivityLabels: Record<string, string> = {
  ask_evaluator: "调用评估助手",
  ask_learning_assistant: "调用助学助手",
  ask_teaching_assistant: "调用助教助手",
  create_task_plan: "制定协作计划",
  detect_intent: "识别学习意图",
  detect_level: "判断学习阶段",
  search_knowledge: "检索课程知识库",
  submit_evaluation: "提交学习评估",
  shell: "运行终端命令",
};

export function toolActivityLabel(toolName: string): string {
  return toolActivityLabels[toolName] ?? toolName;
}
