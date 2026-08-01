# 统一 ReAct Agent 实现

四个角色共用 `ReActAgentNode`，仅角色标识和 Prompt 不同。

## 核心流程

1. 模型读取系统 Prompt、会话历史和已有 Observation。
2. 模型直接回答，或生成 LangChain Tool Call。
3. `ToolExecutor` 执行工具并生成 `ToolMessage` 与 `ToolResult`。
4. Agent 将 Observation 交回模型，直到得到最终回答或达到循环上限。

## 文件

- `src/core/nodes/react_agent.py`：通用 ReAct 循环。
- `src/core/nodes/prompts.py`：四个极简角色 Prompt。
- `src/core/nodes/factory.py`：共享模型、工具执行器和循环配置。
- `src/core/tools/executor.py`：最小工具执行与审计。
- `src/core/models/deepseek.py`：从项目 `.env` 创建 DeepSeek 模型。
- `src/core/graph_builder.py`：Supervisor 与 Worker 的 LangGraph 编排。
- `scripts/verify_deepseek_react.py`：真实 DeepSeek 工具调用验证。

## 验证脚本

```powershell
$env:PYTHONPATH='D:\CODE\Agents\backend\src'
python scripts/verify_deepseek_react.py
```

脚本要求 `.env` 提供 `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` 和
`DEEPSEEK_API_KEY`，执行过程中不会打印 API Key。
