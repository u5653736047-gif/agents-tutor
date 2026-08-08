// D4-T4:快捷指令(/explain /quiz /path)注册表与候选匹配纯函数。
// 指令前缀随消息一并发出,由 Supervisor 侧 Prompt 消费,本期前端
// 不做后端解析——候选列表只是输入辅助(补全/提示),不拦截发送。
// 纯函数抽离便于 SSR 环境直接单测(组件交互无法在
// renderToStaticMarkup 下触发,与 D4-T3 的 clampTextareaHeight 同理)。

export type SlashCommand = {
  name: string; // 如 "explain"(不含斜杠)
  description: string; // 中文描述,展示于候选列表
  example: string; // 完整示例,如 "/explain 支持向量机"
};

export const SLASH_COMMANDS: SlashCommand[] = [
  { name: "explain", description: "深度讲解某个概念", example: "/explain 支持向量机" },
  { name: "quiz", description: "针对某个主题出题", example: "/quiz 卷积神经网络" },
  { name: "path", description: "规划一条学习路径", example: "/path 从零学机器学习" },
];

// 是否处于候选打开态:以 "/" 开头、"/" 后紧跟字母,且尚未输入
// 空格(输入 "/quiz " 表示用户已确认指令边界,候选关闭)。
export function isSlashCandidate(input: string): boolean {
  if (!input.startsWith("/") || input.length < 2) {
    return false;
  }
  const firstChar = input[1];
  if (!firstChar || !/[a-zA-Z]/.test(firstChar)) {
    return false;
  }
  return !input.includes(" ");
}

// 按 "/" 后的前缀过滤指令,大小写不敏感(如 "/Q" 匹配 quiz)。
// 非候选态(不以 "/" 开头/未紧跟字母/已输入空格)恒返回空数组。
export function filterCommands(input: string): SlashCommand[] {
  if (!isSlashCandidate(input)) {
    return [];
  }
  const prefix = input.slice(1).toLowerCase();
  return SLASH_COMMANDS.filter((command) => command.name.startsWith(prefix));
}

// 把输入里的 "/前缀" 整体替换为 "/command.name",保留空格后的内容
// (如 "/" → "/explain"、"/q" → "/quiz"、"/p 支持向量机" →
// "/path 支持向量机")。调用前提:输入以 "/" 开头(UI 仅在候选
// 列表打开时调用),不在此处做候选态校验。
export function applyCommand(input: string, command: SlashCommand): string {
  const spaceIndex = input.indexOf(" ");
  const rest = spaceIndex === -1 ? "" : input.slice(spaceIndex);
  return `/${command.name}${rest}`;
}
