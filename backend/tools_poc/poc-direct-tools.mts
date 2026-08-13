/**
 * POC 1: 直接使用 pi 的工具工厂（不经过 LLM、不创建 agent session）
 *
 * 这是「只把 pi 的 read/bash/edit/write 当工具库用」的最小验证：
 * pi 的 SDK 导出了独立工具工厂 createReadTool / createBashTool /
 * createEditTool / createWriteTool，每个工具就是一个普通对象，
 * 可以脱离 pi 的 agent 循环单独调用 execute()。
 *
 * 对应自研应用的集成点：把工具工厂包装成 LangChain BaseTool
 * （见 README.md 的桥接方案），或直接在 Node 侧使用。
 */
import {
  createBashTool,
  createEditTool,
  createReadTool,
  createWriteTool,
} from "@earendil-works/pi-coding-agent";

const cwd = process.cwd();
console.log(`[poc-direct-tools] cwd = ${cwd}\n`);

// 1. 创建 4 个 pi 内置工具
const read = createReadTool(cwd);
const bash = createBashTool(cwd, {
  // Windows 下 pi 默认找 Git Bash / WSL bash；这里显式指定，避免依赖环境
  shellPath: process.env.PI_SHELL_PATH ?? undefined,
});
const edit = createEditTool(cwd);
const write = createWriteTool(cwd);

console.log("[1] 工具已创建:", [read.name, bash.name, edit.name, write.name].join(", "));

// 2. write: 写一个新文件
const target = "poc-hello.txt";
const writeResult = await write.execute("call-1", {
  path: target,
  content: "hello from pi write tool\nsecond line\n",
});
console.log("\n[2] write.execute ->", JSON.stringify(writeResult.content));

// 3. read: 读回刚才的文件
const readResult = await read.execute("call-2", { path: target });
console.log("[3] read.execute ->", JSON.stringify(readResult.content));

// 4. edit: 精确替换文本（返回统一补丁 details.patch）
const editResult = await edit.execute("call-3", {
  path: target,
  edits: [{ oldText: "hello from pi write tool", newText: "hello from pi EDIT tool" }],
});
console.log("[4] edit.execute ->", JSON.stringify(editResult.content));
console.log("    details.patch ->\n" + editResult.details?.patch);

// 5. bash: 执行 shell 命令验证文件内容
const bashResult = await bash.execute("call-4", { command: "cat poc-hello.txt" });
console.log("[5] bash.execute ->", JSON.stringify(bashResult.content));

// 6. 清理
await bash.execute("call-5", { command: "rm -f poc-hello.txt" });
console.log("\n[poc-direct-tools] 全部通过 ✔");
