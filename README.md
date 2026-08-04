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

## 阶段三骨架联调

先准备两个运行时：

```powershell
cd backend
uv sync --extra dev
cd ..\frontend
npm install
```

根目录 `.env` 需要以下变量。启动脚本只将这些变量传给子进程，绝不会打印
`DEEPSEEK_API_KEY`。

| 变量 | 说明 |
| --- | --- |
| `DEEPSEEK_MODEL` | 必填，DeepSeek 模型名。 |
| `DEEPSEEK_BASE_URL` | 必填，OpenAI 兼容 API 根地址。 |
| `DEEPSEEK_API_KEY` | 必填，真实 DeepSeek 凭证。 |
| `NEXT_PUBLIC_API_BASE_URL` | 可选，前端 API 地址；默认 `http://127.0.0.1:8000`。 |
| `API_SESSION_STORE_PATH` | 可选，会话 SQLite 文件；默认根目录 `data/api_sessions.sqlite3`。 |
| `API_CHECKPOINT_PATH` | 可选，图 checkpoint SQLite 文件；默认根目录 `data/api_checkpoints.sqlite3`。 |
| `API_KNOWLEDGE_DB_PATH` | 可选，知识库词法 SQLite 文件；默认根目录 `data/knowledge.db`（由 ingest 脚本生成，永不降级的底线检索）。 |
| `API_VECTOR_DB_PATH` | 可选，知识库向量 SQLite 文件；默认根目录 `data/vector_knowledge.db`；不可用（文件不存在 / 维度不匹配 / 损坏）时自动降级为纯词法检索，不阻断启动。 |
| `API_KNOWLEDGE_EMBEDDING` | 可选，向量 embedding 提供方模式：`auto`（默认，优先 fastembed 真实语义模型，未安装时自动回退零依赖哈希）或 `hash`（强制哈希，完全离线部署用）。 |

### 启用真实语义检索（可选）

默认 `uv sync --extra dev` 不安装 fastembed，`auto` 模式会自动回退到
零依赖的哈希向量（256 维字符特征替身，语义能力有限）。需要真实语义
检索（fastembed + bge-small-zh-v1.5，512 维）时，在 `backend/` 下安装
可选依赖并重建向量库：

```powershell
uv sync --extra embedding   # 或对已存在的环境：uv pip install fastembed
uv run python scripts/ingest_books.py --force --vector --provider fastembed
```

注意：fastembed 的向量是 512 维，与哈希库的 256 维不同——重建前请先
删除旧 `data/vector_knowledge.db`（或用新的 `--vector-db` 路径），
`--force` 无法绕过维度守卫。

语义检索是否真的在线，看启动日志（`知识检索模式=hybrid
embedding_provider=FastEmbedProvider vector_dimension=512`）或
`GET /healthz` 返回的 `retrieval` 字段：`mode` 为 `hybrid` /
`lexical_only`，`embedding_provider` 为实际打开向量库的 provider 类名，
`vector_dimension` 为其维度。`mode=hybrid` 且
`embedding_provider=FastEmbedProvider` 即真实语义检索在线；否则是哈希
替身或纯词法降级。诊断字段不含任何文件路径。

然后在 Windows PowerShell 的仓库根目录运行一条命令：

```powershell
.\scripts\start-stage3.ps1
```

脚本会加载根目录 `.env`、等待 API `healthz` 与前端就绪，并在 Ctrl+C 时停止两端。
浏览器打开 `http://127.0.0.1:3000`；前端固定使用演示用户 `demo-user`（请求头
`X-User-Id`），API 文档位于 `http://127.0.0.1:8000/docs`。

骨架期的手动验收路径是：在前端创建会话并提问，确认带角色徽章的回答；在 API
文档中用同一 `demo-user` 和会话 ID 查询 / 提交 handoff 的 `confirm` 或 `reject`，
再刷新前端验证历史，最后归档会话。审批卡片的完整前端交互属于后续细节清单。

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

知识层以 SQLite 词法索引为基础（默认 `data/knowledge.db`，由 ingest 脚本
生成，永不降级的底线检索），按查询词与分块内容的命中情况打分排序。
`--vector` 可选生成向量索引（默认 `data/vector_knowledge.db`），可用时默认
检索路径为词法 + 向量混合：两路结果按 RRF 融合排序（S3-T5）；向量索引
不可用（文件缺失 / 维度不匹配 / 损坏）时自动降级为纯词法检索，不阻断
启动——相关环境变量见上文表格 `API_KNOWLEDGE_*`。调用方加载或直接构造
`KnowledgeDocument`，交给 `KnowledgeService`，再通过
`create_search_knowledge_tool()` 封装为 `search_knowledge` 工具。接入 Graph 时
应使用 `tool_permissions` 显式授权助教、助学和评价角色；零命中只返回空结果，
不会生成 Citation。同一 `document_id` 重导入会替换旧分块；同一 PDF 的多页应在
一次 `add_documents()` 调用中提交。

核心架构说明见
[`backend/AGENT_NODE_IMPLEMENTATION.md`](backend/AGENT_NODE_IMPLEMENTATION.md)。
