# 统一 ReAct Agent 实现

四个角色共用 `ReActAgentNode`，仅角色标识和 Prompt 不同。

## 核心流程

1. 模型读取系统 Prompt、会话历史和已有 Observation。
2. 模型直接回答，或生成 LangChain Tool Call。
3. `ToolExecutor` 执行工具并生成 `ToolMessage` 与 `ToolResult`。
4. Agent 将 Observation 交回模型，直到得到最终回答或达到循环上限。

运行事件只记录安全运行元数据，不记录消息正文、工具参数或密钥。
`ToolRegistry` 保证工具名唯一并在执行时检查角色权限；Graph 对 handoff 和
Agent 切换次数设置显式上限。

配置 checkpointer 后，Graph 使用 `user_id + session_id` 组成独立 thread id，
同一会话会继续累积消息、事件和工具结果；新一轮只重置路由错误与次数限制。
`get_state()`、`get_history()` 也仅在配置 checkpointer 后可用。

只有设置 `max_context_messages` 才会在模型调用前裁剪历史，checkpoint 中的完整
消息不变。该值是目标值：为保留最近用户消息和完整 Tool Call/ToolMessage 组，
实际窗口可能略大；没有对应 Tool Call 的孤立 `ToolMessage` 会被丢弃。

## 文件

- `src/core/nodes/react_agent.py`：通用 ReAct 循环。
- `src/core/nodes/prompts.py`：四个极简角色 Prompt。
- `src/core/nodes/factory.py`：共享模型、工具执行器和循环配置。
- `src/core/tools/registry.py`：工具唯一注册与角色权限。
- `src/core/tools/executor.py`：工具执行、错误分类与审计。
- `src/core/events.py`：安全、精简的结构化运行事件。
- `src/core/context.py`：结构完整的模型上下文窗口。
- `src/core/persistence.py`：SQLite checkpointer 生命周期封装。
- `src/core/sessions.py`：独立的会话元数据与归档状态。
- `src/core/knowledge/loaders.py`：UTF-8 文本与逐页 PDF 加载。
- `src/core/knowledge/chunking.py`：确定性的重叠字符分块。
- `src/core/knowledge/index.py`：可替换索引协议与内存词法索引。
- `src/core/knowledge/service.py`：知识写入、删除和 Top-K 检索。
- `src/core/knowledge/tools.py`：无全局状态的 `search_knowledge` 工具工厂。
- `src/core/models/deepseek.py`：从项目 `.env` 创建 DeepSeek 模型。
- `src/core/graph_builder.py`：Supervisor 与 Worker 的 LangGraph 编排。
- `scripts/verify_deepseek_react.py`：真实 DeepSeek 工具调用验证。

## 验证脚本

```powershell
$env:PYTHONPATH="$PWD\src"
python scripts/verify_deepseek_react.py
```

脚本要求 `.env` 提供 `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` 和
`DEEPSEEK_API_KEY`，执行过程中不会打印 API Key。

### 2026-08-02 真实 DeepSeek 冒烟

在允许外部网络访问的环境中运行验证脚本，脱敏日志如下：

```text
model=deepseek-v4-flash
iterations=2
tools=['double']
answer=42
```

结论：真实模型完成两轮 ReAct，先调用 `double(21)`，读取工具结果后输出 `42`；
模型决策、工具调用与最终回答链路均通过，运行过程未输出 API Key。

## 知识工具接入

`create_search_knowledge_tool(service)` 返回普通 LangChain 工具。现有
`ToolExecutor` 会把命中内容、分数和 Citation 字典转换为 JSON Observation，
无需修改 ReAct 循环。Graph 对业务工具默认允许全部角色，因此接入时必须通过
`tool_permissions` 显式排除 Supervisor。该工具只保证零命中时不伪造引用，不
负责校验模型最终回答中的自由文本引用。
