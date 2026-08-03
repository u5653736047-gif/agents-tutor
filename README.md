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
- 文本/PDF 加载、确定性分块、内存检索与可追溯引用工具
- Python 代码沙箱执行、LaTeX 公式校验/MathML 转换、SQLite 学习记录读写工具
- 作业批改闭环：PDF 上传解析、客观题自动批阅 + 主观题评分建议、学情诊断
- 备课素材：测验/教案结构化骨架生成，内容由 Agent 模型填充
- 工具级超时控制，超时归类为 `TOOL_TIMEOUT` 结构化错误
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

## 知识检索

当前知识层使用轻量内存索引，无需学科文档或向量数据库即可测试。调用方加载或
直接构造 `KnowledgeDocument`，交给 `KnowledgeService`，再通过
`create_search_knowledge_tool()` 封装为 `search_knowledge` 工具。接入 Graph 时
应使用 `tool_permissions` 显式授权助教、助学和评价角色；零命中只返回空结果，
不会生成 Citation。内存索引不提供跨进程持久化，后续可按 `KnowledgeIndex`
协议替换实现。同一 `document_id` 重导入会替换旧分块；同一 PDF 的多页应在
一次 `add_documents()` 调用中提交。

核心架构说明见
[`backend/AGENT_NODE_IMPLEMENTATION.md`](backend/AGENT_NODE_IMPLEMENTATION.md)。
