# 多智能体助教助学系统

基于 LangGraph 的教学多智能体项目。当前四个角色共用同一个简易
`ReActAgentNode`，仅角色标识与极简 Prompt 不同。

## 当前能力

- Supervisor、助教、助学、评价四个同构 Agent
- “模型决策 → 工具执行 → 结果观察”ReAct 循环
- Supervisor 通过 `handoff` 工具进行节点路由
- 工具名称唯一校验、角色权限和结构化错误
- 安全运行事件以及 handoff、Agent 切换上限
- 可选 SQLite 持久化，按 `user_id + session_id` 逻辑分区
- 可选模型上下文裁剪，checkpoint 仍保留完整历史
- 使用项目根目录 `.env` 接入 DeepSeek
- pytest、Ruff 和 mypy 质量检查

## 环境准备

项目要求 Python 3.11 或更高版本，推荐使用 uv：

```powershell
cd backend
uv sync --extra dev
```

复制 `.env.example` 为 `.env`，然后填入自己的 DeepSeek 配置。`.env`
已被 Git 忽略，请勿提交真实 API Key。

## 验证

```powershell
$env:PYTHONPATH="$PWD\src"
uv run pytest tests -q
uv run ruff check src tests scripts
uv run mypy src/core
uv run python scripts/verify_deepseek_react.py
```

最后一条命令会发起一次真实 DeepSeek Tool Call，其余测试不会访问模型 API。

SQLite 持久化由调用方显式传入数据库路径，并在上下文管理器内使用：

```python
with open_sqlite_checkpointer("data/checkpoints.sqlite") as saver:
    graph = CollaborativeAgentGraph(model=model, checkpointer=saver)
    graph.run("你好", session_id="lesson-1", user_id="user-1")
```

`SessionStore` 独立保存 `user_id`、`session_id`、创建时间和归档状态，不会与
Graph 自动同步。归档只让默认会话列表隐藏该记录，不会删除 checkpoint，也不
会阻止 Graph 继续使用同一会话。

核心架构说明见
[`backend/AGENT_NODE_IMPLEMENTATION.md`](backend/AGENT_NODE_IMPLEMENTATION.md)。
