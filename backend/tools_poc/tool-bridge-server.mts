/**
 * POC 3: 工具桥接服务 —— 把 pi 的 4 个基础工具暴露为 JSON-RPC over stdio
 *
 * 适用场景：自研 agent 是 Python（LangGraph），pi 是 Node 库。
 * 本服务作为常驻子进程，Python 侧通过 stdin/stdout 的 JSON-RPC 调用
 * read/bash/edit/write，拿到的工具能力与 pi 内部完全一致
 * （截断、diff/patch、超时、输出流式回调）。
 *
 * 协议（每行一个 JSON 对象）：
 *   请求:  {"id": 1, "method": "read",  "params": {"path": "a.txt"}}
 *   响应:  {"id": 1, "result": {"content": "...", "details": {...}}}
 *   错误:  {"id": 1, "error": {"message": "..."}}
 *   通知:  {"method": "update", "params": {"id": 1, "text": "..."}}   // bash 流式输出
 */
import { createBashTool, createEditTool, createReadTool, createWriteTool } from "@earendil-works/pi-coding-agent";
import { createInterface } from "node:readline";
import type { AgentTool } from "@earendil-works/pi-agent-core";

const cwd = process.cwd();
const shellPath = process.env.PI_SHELL_PATH ?? undefined;

const tools: Record<string, AgentTool<any>> = {
  read: createReadTool(cwd),
  bash: createBashTool(cwd, { shellPath }),
  edit: createEditTool(cwd),
  write: createWriteTool(cwd),
};

/** bash 工具的流式输出通过 onUpdate 回调转成通知发给调用方 */
function makeOnUpdate(id: number) {
  return (_callId: string, update: { content?: Array<{ type: string; text?: string }> } | undefined) => {
    if (!update) return;
    const text = update.content?.map((c) => ("text" in c && c.text) || "").join("") ?? "";
    if (text) writeLine({ method: "update", params: { id, text } });
  };
}

function writeLine(obj: unknown) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on("line", async (line) => {
  let req: { id?: number; method?: string; params?: Record<string, unknown> };
  try {
    req = JSON.parse(line);
  } catch {
    writeLine({ error: { message: "invalid JSON" } });
    return;
  }
  if (!req.method) return; // 无 method 视为心跳/空行

  const tool = tools[req.method];
  if (!tool) {
    writeLine({ id: req.id, error: { message: `unknown tool: ${req.method}` } });
    return;
  }
  const args = (req.params ?? {}) as Record<string, unknown>;
  try {
    const result = await tool.execute(String(req.id ?? 0), args, undefined, makeOnUpdate(req.id ?? 0));
    // 内容统一转文本（图片场景本项目暂不需要）
    const text = (result.content ?? [])
      .map((c) => (c && c.type === "text" && "text" in c ? c.text : ""))
      .join("");
    writeLine({ id: req.id, result: { text, details: result.details ?? {} } });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    writeLine({ id: req.id, error: { message } });
  }
});

writeLine({ result: { ready: true, tools: Object.keys(tools), cwd } });
