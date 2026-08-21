# 多智能体助教助学系统

基于 LangGraph 的教学多智能体项目。当前四个角色共用同一个简易
`ReActAgentNode`，仅角色标识与极简 Prompt 不同。

## 当前能力

- Supervisor、助教、助学、评价四个同构 Agent
- “模型决策 → 工具执行 → 结果观察”ReAct 循环
- 生产链路以 Supervisor 为主智能体，通过 `ask_*` 工具等待专业 Agent 完成并整合结果
- tool 模式任务计划：复杂请求可由 Supervisor 调用 `create_task_plan` 建立有序计划，核心层确定性门控逐步执行（乱序拒绝、失败策略 abort/continue/retry、重试预算有界），计划与逐步结果经响应字段与回放接口可见
- SSE 原生 token 流，以及思考摘要、工具调用和子代理阶段输出
- “思考”仅展示可审计的执行摘要，不公开模型原始推理文本或工具参数/结果正文
- 兼容原有 `handoff` / 人工审批编排模式（非生产默认）
- 工具名称唯一校验、角色权限和结构化错误
- 每会话可选择主工作区并追加授权根目录；只读文件工具支持相对路径与已授权绝对路径，含路径逃逸、链接越界与敏感文件防护
- `inspect_workspace` 可把多项只读检查合并为一次工具调用；Supervisor 还可提出一条复合 Shell 命令，经用户逐次审批后实时回显 stdout/stderr
- 可选 officecli 集成：`officecli_inspect` 只读查看、`officecli_edit` 经人工审批后修改工作区内 `.docx/.xlsx/.pptx`（默认禁用，见「Office 文档工具」一节）
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
| `API_KNOWLEDGE_REWRITE` | 可选，LLM 查询改写开关：`auto`（默认，已配置 `DEEPSEEK_API_KEY` 时启用——把一个查询改写成多个检索变体联合检索提升召回）或 `off`（强制关闭）；未配置 key 时 `auto` 自动跳过。 |
| `API_KNOWLEDGE_RERANK` | 可选，Cross-Encoder 重排开关：`auto`（默认，fastembed 可用时装配 bge-reranker，对初检候选精排；构造失败自动降级为不重排）或 `off`（强制关闭）。 |
| `API_RERANK_MODEL` | 可选，重排模型名；默认 `BAAI/bge-reranker-base`（首次启用需联网下载模型约 280MB，之后离线）。 |
| `API_PDF_TABLE_MODE` | 可选，PDF 表格结构化提取：`auto`（默认，已安装 `pdf-table` 依赖组时启用——附件与入库两条链路的表格转 GFM Markdown）或 `off`；未装依赖时自动回退 pypdf 纯文本。存量知识库需 `--force` 重入库才能获得表格增强。 |
| `API_VISION_MODE` | 可选，图片理解视觉端点：`auto`（默认，三项视觉 env 均配置才启用，作为三级降级链第一级：VLM → OCR → 友好提示）或 `off`。DeepSeek 主站不支持图片输入，面向自选 OpenAI 兼容端点（如 Qwen-VL）。 |
| `API_VISION_BASE_URL` / `API_VISION_MODEL` / `API_VISION_API_KEY` | 可选，视觉端点三元组；调用预算 timeout=10s / max_retries=0 / max_tokens=512。 |
| `API_WORKSPACE_ROOT` | 可选，新会话的默认主工作区；启动脚本默认绑定当前仓库根。用户可在创建会话时选择其他目录。 |
| `API_WORKSPACE_ALLOWED_ROOTS` | 可选，部署侧允许用户选择的目录边界；多个根用系统 PATH 分隔符分隔（Windows `;`、Linux/macOS `:`）。未设置时本地模式允许明确选择任意现有目录；生产部署建议必设。 |
| `API_OFFICECLI_ENABLED` | 可选，默认 `0`（不注册 office 工具、不探测二进制）；显式设 `1` 才启用，启用时启动自检失败会中止启动。 |
| `API_OFFICECLI_BINARY` | 可选，officecli 二进制名或绝对路径；默认 `officecli`（PATH 查找）。 |
| `API_OFFICECLI_TIMEOUT_READ_SECONDS` | 可选，只读工具子进程超时；默认 `60`。 |
| `API_OFFICECLI_TIMEOUT_WRITE_SECONDS` | 可选，写工具子进程超时；默认 `120`。 |
| `API_OFFICECLI_MAX_OUTPUT_BYTES` | 可选，单次 stdout+stderr 合并上限；默认 `131072`。 |

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
`vector_dimension` 为其维度；`rewrite_enabled` / `reranker_enabled`
分别表示 LLM 查询改写与 Cross-Encoder 重排是否启用。`mode=hybrid` 且
`embedding_provider=FastEmbedProvider` 即真实语义检索在线；否则是哈希
替身或纯词法降级。诊断字段不含任何文件路径。

然后在 Windows PowerShell 的仓库根目录运行一条命令：

```powershell
.\scripts\start-stage3.ps1
```

脚本会加载根目录 `.env`、等待 API `healthz` 与前端就绪，并在 Ctrl+C 时停止两端。
浏览器打开 `http://127.0.0.1:3000`；前端固定使用演示用户 `demo-user`（请求头
`X-User-Id`），API 文档位于 `http://127.0.0.1:8000/docs`。

手动验收路径：在前端创建会话并提问，确认 Supervisor 的主回答按 token 增量出现；
需要专业 Agent 时，Supervisor 会在同一轮内等待其完成并整合结果，不提前结束会话。
可在新建会话时输入或浏览服务器上的工作区，也可为当前会话追加授权根；文件工具既可使用
相对路径，也可使用这些根目录内的绝对路径。遇到适合终端处理的任务时，页面应先展示完整
命令、工作目录与超时，批准后在同一工具卡片内逐段显示 stdout/stderr，随后继续生成最终回答。
最后切换或刷新页面验证权威全文仍在历史中，再归档会话。模型或编排失败时，对话区应
显示稳定错误提示，不能只结束加载状态而没有回答。

## Office 文档工具（officecli，可选）

以 CLI 子进程方式集成 officecli，为四个角色提供
Office 文档读写能力（设计决策与威胁模型见 `docs/officecli-integration-plan.md`）：

| 工具 | 用途 | 审批 | 角色 |
| --- | --- | --- | --- |
| `officecli_inspect` | 只读：help / load_skill / view / get / query / validate | 不需要 | 四个角色均可用 |
| `officecli_edit` | 写操作：create / set / add / remove / move / swap / batch / import / merge | 必须人工审批 | Supervisor、助教、评价 |

安全要点：动词/选项/batch 子命令三层白名单（默认拒绝）；文件参数与 `--prop`
中的文件引用全部限定在当前会话工作区内并重写为授权绝对路径；子进程 stdin
始终关闭、不弹窗、禁后台更新与 resident；同一文件的读写命令由 per-file 锁
串行（仅单进程语义，多 worker 部署不在本期范围）。

启用步骤：

1. 在部署机安装 officecli 并确认 `officecli --version` 可用（基线版本
   1.0.144，不一致时启动日志只告警不阻断）；
2. `.env` 中设 `API_OFFICECLI_ENABLED=1`（其余 `API_OFFICECLI_*` 变量见上表）；
3. 启动后端。找不到二进制或自检失败会 fail-fast；未启用时完全不探测二进制。

验收：

```powershell
cd backend
uv run python scripts/verify_officecli_integration.py   # 输出 PASS 即全链路可用
```

本期非目标：HTML/截图预览与 `watch` 实时预览不接入（`view` 仅文本模式）；
PDF 导出、MCP 适配器、`raw`/`dump`/`plugins` 等命令不在白名单内。

## 容器启动（Docker Compose）

> ⚠️ **验收状态**：本小节由静态审查交付（开发环境无 Docker，未能执行
> `docker compose up -d` 实测）。YAML/Dockerfile 经逐行人工校验，但
> 首次使用请按下方启动步骤自行验证；若发现与描述不符，以实测为准
> 并回报修正。

在具备 Docker 的环境下，可用 Compose 一键拉起前后端，无需本机安装
Python/Node。仓库根目录的 `docker-compose.yml` 编排两个服务：

| 服务 | 镜像来源 | 对外端口 | 说明 |
| --- | --- | --- | --- |
| `api` | `backend/Dockerfile`（python:3.11-slim） | `8000` | FastAPI + uvicorn，已含 `[embedding]` 可选组（fastembed，语义检索开箱可用） |
| `frontend` | `frontend/Dockerfile`（node:22-alpine） | `3000` | Next.js 生产构建（`next start`） |

启动前先在仓库根目录配置 `.env`（复制 `.env.example` 并填入
`DEEPSEEK_API_KEY`），然后执行：

```bash
docker compose up -d --build
```

浏览器打开 `http://localhost:3000`，API 文档在 `http://localhost:8000/docs`。
停止与清理：

```bash
docker compose down      # 停止服务；data/ 数据卷保留
docker compose down -v   # 停止并删除卷（连同 data/ 数据，慎用）
```

### 容器版环境变量

与骨架联调一致：密钥与可选配置都来自根目录 `.env`，compose 只做 `${VAR}`
占位透传，不写死任何密钥（`.env` 已被 Git 忽略）。

| 变量 | 容器内行为 |
| --- | --- |
| `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` | 必填，从 `.env` 透传；`DEEPSEEK_API_KEY` 缺失时 `docker compose up` 直接报错 |
| `API_SESSION_STORE_PATH` 等 5 个 `API_*_PATH` | 容器内固定指向挂载卷 `/app/data/...`。后端默认路径按宿主仓库根解析（`__file__.parents[3]`），容器里会落到不可写的根目录 `/`，必须显式指定；宿主 `.env` 里的 Windows 风格路径对容器无效 |
| `API_KNOWLEDGE_EMBEDDING` | 可选，默认 `auto`；镜像已装 fastembed，`auto` 即真实语义检索 |
| `API_WORKSPACE_ROOT` / `API_WORKSPACE_ALLOWED_ROOTS` | 均固定为容器内 `/workspace`；选择器只能授权该挂载内目录。宿主其他目录必须先显式挂载，不能直接用宿主绝对路径。 |
| `NEXT_PUBLIC_API_BASE_URL` | 前端构建参数，默认 `http://localhost:8000`（见下方说明） |

### 数据卷与前端 API 地址说明

- **数据卷**：`api` 服务的 `./data:/app/data` 把 SQLite 会话、checkpoint、
  知识库与反馈文件都保留在宿主机 `data/`，`docker compose down` 后数据
  仍在；`data/knowledge.db` 等可直接复用宿主机现有产物，重建知识库用
  宿主机脚本 `backend/scripts/ingest_books.py`。注意：若现有向量库维度与
  容器内 fastembed 不匹配（如宿主是 256 维哈希库），检索会自动降级为纯
  词法，不阻断启动。
- **Agent 工作区**：宿主仓库通过 `./:/workspace:ro` 只读挂载。文件工具包括
  `workspace_info`、`list_files`、`glob_files`、`grep_files`、`read_file` 与
  `inspect_workspace`，可使用相对路径或已授权的容器内绝对路径。Supervisor 可提出
  支持管道/顺序语句的前台 Shell 命令，但每次都必须由用户核对完整命令并批准；终端
  输出会实时归入同一工具卡片。Shell 以 API 服务账号权限运行，工作目录授权不是操作
  系统级命令沙箱；容器中的 `/workspace` 只读挂载可防止其修改仓库，但命令仍可能访问
  服务账号可访问的其他容器资源，因此只应批准可信命令。
- **前端 API 地址**：`NEXT_PUBLIC_*` 由 Next.js 在构建时内联进浏览器产物，
  运行时改环境变量无效，所以 compose 用 build args 传入
  `http://localhost:8000`（宿主端口映射到 `api` 容器）。不能传 compose
  内网服务名 `http://api:8000`——那是浏览器里 fetch 的目标地址，用户
  浏览器解析不了 `api` 主机名；服务间调用（如 healthcheck）才用服务名。
  已知限制：首页的连接状态徽标由**服务端组件** `app/page.tsx` 在容器内
  fetch `/healthz` 得出，容器内 `localhost` 指向容器自身——因此容器部署
  下首页徽标可能显示「后端暂不可用」，但浏览器端实际请求（对话/检索等）
  仍走 `8000:8000` 映射正常可用。如需徽标准确，可设
  `SERVER_API_BASE_URL=http://api:8000` 并让 `app/page.tsx` 使用它
  （当前未实现，列为后续优化）。

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
启动——相关环境变量见上文表格 `API_KNOWLEDGE_*`。混合初检之上还有两个
可选增强（S5）：LLM 查询改写（`API_KNOWLEDGE_REWRITE`，一个查询改写为
多个变体联合检索，原查询永远参与、改写失败自动降级单路）与
Cross-Encoder 重排（`API_KNOWLEDGE_RERANK`，对初检候选窗口精排，只改
顺序不改分数）。调用方加载或直接构造
`KnowledgeDocument`，交给 `KnowledgeService`，再通过
`create_search_knowledge_tool()` 封装为 `search_knowledge` 工具。接入 Graph 时
应使用 `tool_permissions` 显式授权助教、助学和评价角色；零命中只返回空结果，
不会生成 Citation。同一 `document_id` 重导入会替换旧分块；同一 PDF 的多页应在
一次 `add_documents()` 调用中提交。检索质量的离线评测（Recall@K / MRR 三
配置对比）见 `backend/scripts/evaluate_retrieval.py`，报告产物归档在
`docs/perf-evidence/`。

核心架构说明见
[`backend/AGENT_NODE_IMPLEMENTATION.md`](backend/AGENT_NODE_IMPLEMENTATION.md)。
