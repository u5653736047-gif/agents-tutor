# pi 基础工具集成 POC（read / bash / edit / write）

把 pi 的 4 个内置基础工具集成进自研多智能体应用（`D:\CODE\Agents`）的验证项目。

## 背景结论（调研摘要）

- pi 的 SDK（`@earendil-works/pi-coding-agent`，已装版本 0.84.1）同时提供两条集成路径：
  1. **整个 agent 会话**：`createAgentSession({ tools: ["read","bash","edit","write"] })`
     —— 推文方案，工具 + pi 的 ReAct 循环 + 会话管理一起用；
  2. **只用工具**：`createReadTool / createBashTool / createEditTool / createWriteTool`
     —— 每个工具是普通对象，可脱离 pi 的 agent 循环直接 `execute()` 调用。
- 每个工具还支持**后端注入**（`operations` 选项：`ReadOperations` / `WriteOperations` /
  `EditOperations` / `BashOperations`），可以把文件读写/命令执行委托给自定义后端
  （如 SSH、沙箱、审批门控）。
- 自研项目是 **Python + LangGraph**，pi 是 **Node** 库，因此集成有两个层次的选择：
  - **轻集成（推荐先做）**：Node 桥接进程（本目录 `tool-bridge-server.mts`），
    Python 侧把 4 个工具包成 LangChain `BaseTool`，用 `subprocess`/HTTP 调用；
  - **重集成**：整条 agent 循环换成 pi 的 `createAgentSession`（相当于放弃自研
    ReAct 循环与 LangGraph 编排，只保留业务工具作为 `customTools`）。
- pi 工具的优势：read 的截断/续读、edit 的 diff + unified patch、bash 的流式输出/
  超时/进程树清理、统一参数 schema。自研工具的优势：工作区沙箱（路径逃逸防护）、
  shell 人工审批、角色权限（`ToolRegistry` + `AgentRole`）、稳定错误码。
  **注意**：pi 的 bash 工具没有审批门控、read/edit/write 也没有路径沙箱，
  生产环境需要在自己的桥接层/工具层补上（或利用 `operations` 注入实现）。

## 本机环境注意（重要）

Windows 上 pi 的 bash 工具按以下顺序找 shell：settings 的 `shellPath` →
`C:\Program Files\Git\bin\bash.exe` → PATH 上的 `bash.exe`。本机没有 Git Bash，
PATH 上的 `bash.exe` 是 WSL 启动器且未装发行版，导致 bash 工具报
「适用于 Linux 的 Windows 子系统没有已安装的分发版」。

已修复：`~/.pi/agent/settings.json` 中加入
`"shellPath": "D:\\Git\\bin\\bash.exe"`（本机 Git for Windows 装在 D:\Git），
**重启 pi 后生效**。SDK 场景也可在代码里显式传 `shellPath`（见各脚本的
`PI_SHELL_PATH` 环境变量，运行前请设置：`$env:PI_SHELL_PATH="D:\Git\bin\bash.exe"`）。

## 运行

```powershell
cd D:\CODE\Agents\backend\tools_poc
npm install

# POC 1：不经过 LLM，直接验证 4 个工具可独立调用（推荐先跑这个）
$env:PI_SHELL_PATH="D:\Git\bin\bash.exe"
npx tsx poc-direct-tools.mts

# POC 2：官方推文方案的修正版（需模型 API Key，pi 已登录则直接可用）
npx tsx poc-session.mts

# POC 3：工具桥接服务（stdio JSON-RPC），供 Python 侧调用
$env:PI_SHELL_PATH="D:\Git\bin\bash.exe"
npx tsx tool-bridge-server.mts
# 交互测试：echo '{"id":1,"method":"read","params":{"path":"package.json"}}' | npx tsx tool-bridge-server.mts
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `poc-direct-tools.mts` | 直接用工具工厂调用 read/bash/edit/write（无 LLM） |
| `poc-session.mts` | 官方推文代码修正版：`createAgentSession` + `tools` 白名单 |
| `tool-bridge-server.mts` | 把 4 个工具暴露为 stdio JSON-RPC 的桥接服务 |
