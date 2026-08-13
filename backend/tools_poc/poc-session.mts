/**
 * POC 2: 官方推文代码的修正版 —— 用 createAgentSession 创建带
 * read/bash/edit/write 四个内置工具的 agent 会话，并让它实际操作文件。
 *
 * 官方推文原文（含笔误，已修正）：
 *   import { createAgentSession, ModelRuntime ) from "@earendil-works/pi-coding-agent";
 *   const runtime = await ModelRuntime.create();
 *   const { session } = await createAgentSession({
 *     model: runtime.getModel("openai", "gpt-5.6-sol"),
 *     modelRuntime: runtime
 *     tools: ["read", "bash", "edit", "write"],
 *   });
 *   await session.prompt("Fix the failing test in utils.py");
 *
 * 修正点：
 *   1. `) ` → `} `（导入花括号笔误）
 *   2. `modelRuntime: runtime` 后缺逗号
 *   3. 模型改用本机实际可用的模型（默认取 settings 的 opencode-go/deepseek-v4-flash，
 *      找不到则取第一个可用模型），推文里的 "gpt-5.6-sol" 是占位名
 *   4. 加了事件订阅，能看到模型输出与工具调用
 */
import { createAgentSession, ModelRuntime, SessionManager } from "@earendil-works/pi-coding-agent";

const runtime = await ModelRuntime.create();

// 选择模型：优先 opencode-go/deepseek-v4-flash（当前 settings 默认），否则第一个可用
let model = runtime.getModel("opencode-go", "deepseek-v4-flash");
if (!model) {
  const available = await runtime.getAvailable();
  model = available[0];
  if (!model) throw new Error("没有可用模型，请先配置 API Key（pi /login 或 auth.json）");
}
console.log(`[poc-session] 使用模型: ${model.provider}/${model.id}\n`);

// 官方推文代码（修正版）：tools 白名单只开 4 个基础工具
const { session } = await createAgentSession({
  model,
  modelRuntime: runtime,
  tools: ["read", "bash", "edit", "write"],
  sessionManager: SessionManager.inMemory(), // 不落盘，纯验证
  thinkingLevel: "low",
});

try {
  // 订阅事件：打印模型文本流 + 工具调用
  session.subscribe((event) => {
    switch (event.type) {
      case "message_update":
        if (event.assistantMessageEvent.type === "text_delta") {
          process.stdout.write(event.assistantMessageEvent.delta);
        }
        break;
      case "tool_execution_start":
        console.log(`\n[工具调用] ${event.toolName}`);
        break;
      case "tool_execution_end":
        console.log(`[工具结束] ${event.toolName} ${event.isError ? "(失败)" : "(成功)"}`);
        break;
    }
  });

  // 让 agent 实际使用 4 个工具：写文件 → 读文件 → 编辑 → bash 确认
  await session.prompt(
    "请依次完成以下操作并汇报结果：\n" +
      "1. 用 write 创建文件 poc-session-demo.txt，内容为 'hello from pi session\\n'；\n" +
      "2. 用 read 读取该文件；\n" +
      "3. 用 edit 把 'hello' 改为 'hello world'；\n" +
      "4. 用 bash 执行 `cat poc-session-demo.txt` 确认内容；\n" +
      "5. 用 bash 执行 `rm -f poc-session-demo.txt` 清理。",
  );
  console.log("\n\n[poc-session] 完成 ✔");
} finally {
  session.dispose();
}
