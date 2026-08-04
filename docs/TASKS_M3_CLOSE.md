# M3 收口与系统完善任务清单

> 生成时间：2026-08-04
> 适用对象：全新会话的开发 agent。**本清单自包含**，不需要任何历史会话上下文；
> 文中引用的仓库内文件按需阅读即可。
> 目标：补齐产品化最后三个硬化缺口（Sprint H），执行阶段三细节清单达成
> 里程碑 M3，阶段一二收尾项（Sprint 5）作为间隙填充。

---

## 一、项目与系统现状（开工前必读）

**项目**：多智能体助教助学系统（仓库根 `D:/CODE/Agents`）。学生提问 →
Supervisor 路由/分解 → 助教/助学 Worker 检索教材作答 → 评价 Agent 审计，
回答带可追溯引用；FastAPI 后端 + Next.js 前端。

**已建成的能力**（全部有测试与质量门禁保障，不要在不知情的情况下破坏）：

- `backend/src/core/`：LangGraph 四角色同构 ReAct（Supervisor/助教/助学/
  评价）、任务分解与聚合、handoff 审批断点（可恢复）、工具注册/权限/超时/
  错误分类、SQLite checkpointer、Token 预算裁剪、意图识别、分层讲解、
  结构化评价、消息级引用元数据与真实性校验。
- `backend/src/core/knowledge/`：词法索引 + 向量索引（bge-small-zh-v1.5，
  512 维）+ RRF 混合检索、元数据过滤、Query 改写/重排/自适应 RAG 策略
  （编排层已实现，工具层可选装配）；教材已入库 4 本共 5024 chunk
  （`data/knowledge.db` 词法库、`data/vector_knowledge.db` 向量库，
  均不入 git）。已知外部阻塞：第 5 本《机器学习方法》（ml-lihang）是扫描版
  PDF 无文本层，标记 blocked——**不要尝试入库它**。
- `backend/src/api/`：FastAPI 应用（sessions/chat/approvals REST），
  lifespan 已装配知识检索链路（词法 → 向量可选 → 混合 → KnowledgeService
  → `search_knowledge` 工具，授权 learning_assistant + teaching_assistant），
  `ChatResponse.references` 契约已生效。环境变量见 README「阶段三骨架联调」
  一节（含 `API_KNOWLEDGE_DB_PATH` / `API_VECTOR_DB_PATH` /
  `API_KNOWLEDGE_EMBEDDING`）。
- `frontend/`：Next.js（App Router + TS strict + Tailwind + shadcn/ui）
  骨架：会话侧栏、对话界面、Agent 角色徽章、Markdown 渲染、审批确认/拒绝。
  类型由后端 OpenAPI 生成（`frontend/contracts/`，勿手改）。
- `scripts/start-stage3.ps1`：一条命令启动双端；根目录 `.env` 有真实
  DeepSeek 凭证（`DEEPSEEK_MODEL/BASE_URL/API_KEY`）。**任何输出不得打印
  API Key。**

**质量基线（不允许退化）**：后端 627 测试通过、ruff 干净、mypy strict
（35 源文件）零问题；前端 `npm run lint` / `typecheck` / `build` 全绿。

## 二、执行规则

1. 任务来源与勾选位置：
   - Sprint H（本文件新增，3 项）：勾选本文件。
   - Sprint D1–D7（阶段三细节）：执行 `docs/TASKS_STAGE_3_DETAILS.md`，
     勾选该文件；其内部依赖顺序见其第十一节，M3 出口检查见其第十节
     （含总清单勾选同步约定）。
   - Sprint 5（阶段一二收尾，9 项，可选）：执行
     `docs/TASKS_STAGE_1_2.md` 第 620 行起 Sprint 5，勾选该文件。
2. 推进顺序：Sprint H（H-T1 → H-T2）→ D1 → D2 → … → D7 → M3 出口检查。
   H-T3 与 Sprint 5 为可选项，可插入任意间隙。细节清单的 F1–F5 骨架修复项
   在其第九节，按该清单规则处理。
3. 一次只领一个原子任务；每个任务一次独立提交，Conventional Commits 风格。
4. 完成定义 = 实现完成 + 验收标准全部通过 + 质量门禁通过，缺一不可。
5. 质量门禁：
   - 后端（`backend/` 目录，用项目 venv）：
     `PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q`、
     `.venv/Scripts/ruff.exe check src tests`、
     `.venv/Scripts/python.exe -m mypy src`
     （mypy 必须用 python -m 入口，.exe 入口在本机异常）。
   - 前端（`frontend/` 目录）：`npm run lint`、`npm run typecheck`、
     `npm run build`。
6. 架构红线：不改 `core/` 既有逻辑（任务明确要求的纯新增适配除外）；
   core 的同步阻塞调用放工作线程；事件不记录敏感正文；错误只暴露稳定
   错误码；工具权限显式声明；API 契约改动后重新生成前端类型并过
   `npm run typecheck`。
7. 遇到阻塞（缺外部资源、验收冲突、环境装不上依赖）记录原因并停止该任务，
   不伪造验证结果，不自行扩大范围。
8. 细节提醒：多个检索测试对照依赖词法索引的确定性结果，改动索引/分块/
   过滤语义时必须复核受影响测试；`data/` 不入 git。

---

## Sprint H：产品化硬化缺口（新增，先于 D 系列执行）

> 背景：以下三项是最近一轮评估发现的真实缺口，不在任何既有清单中。
> H-T1 / H-T2 直接影响 D6-T4（检索测试面板）呈现出的检索质量，必须先做。

### [x] H-T1 Embedding 部署闭环（语义检索不再静默消失）

- 背景：`data/vector_knowledge.db` 是 512 维（fastembed bge-small-zh-v1.5
  构建），但 fastembed 刻意未写入 pyproject/锁文件（既定决策，见
  `docs/EMBEDDING_SELECTION.md`）。新部署环境未手动安装 fastembed 时，
  API 启动会回退哈希 provider（256 维）→ 打不开 512 维库 → 静默降级为
  纯词法检索，仅在启动日志有一行 info。「语义检索在线」名存实亡且不可诊断。
- 范围：`backend/pyproject.toml`、部署文档（README）、API 启动状态暴露。
- 验收标准：
  - 依赖决策闭环（二选一，书面记录结论）：(a) fastembed 写入 pyproject
    可选依赖组（如 `semantic` extra）并 `uv lock`，README 写明
    `uv sync --extra semantic` 启用步骤；(b) 维持不锁定，README 写明
    `uv pip install fastembed` 的手动步骤。两条路都必须让运维一眼知道
    「怎么启用真实语义检索」。
  - 运行时可观测：启动日志与一个对外诊断途径（扩展 `/healthz` 或新增
    诊断端点）报告当前检索模式（`lexical_only` / `hybrid`）、embedding
    provider 名与向量维度；只暴露模式与维度，不暴露服务器绝对路径。
  - 测试覆盖：配置解析（auto/hash/非法值）、诊断字段、降级路径三态
    （无向量库 / 维度不匹配 / 正常 hybrid）。
- 依赖：无。

### [ ] H-T2 向量检索噪音治理（目录页/前言页）

- 背景：真实模型下向量检索偶见目录页、讨论链接等噪音命中（如 "xvi"、
  "discuss.d2l.ai" 页面与查询向量相似度高），已记录在
  `docs/EMBEDDING_SELECTION.md`「观察 3」。根因是目录行被切成独立 chunk。
- 范围：`backend/src/core/knowledge/`（chunking/ingest 规则 + 检索侧处理）、
  `backend/scripts/ingest_books.py`。
- 验收标准：
  - 规则式识别前言/目录类 chunk（启发式即可，如：页码靠前 + 目录特征行
    ——点线、连续数字结尾、极短行密度），ingest 时写入 chunk metadata
    （键名与值形态须兼容 S3-T3 的 metadata_filter 语义，值用字符串或
    字符串列表）；坐标语义（document_id/page/start/end）不变。
  - 检索侧默认抑制该类 chunk（过滤或降权，方案写明并可通过参数关闭），
    词法/向量/混合三路行为一致；`search_knowledge` 工具路径同步生效。
  - 已入库数据处理：给出不重解析 PDF 的增量更新路径（如读取已有 chunk
    更新 metadata 后 upsert，整文档替换语义保持幂等），并对 4 本书执行；
    词法库与向量库同步更新。
  - 真实复测：查询「卷积网络」「注意力机制」，向量 top15 中目录/讨论链接
    类条目明显减少，且正例（dl-d2l 卷积神经网络章节、dl-goodfellow
    注意力内容）仍命中；结果追加到 `docs/EMBEDDING_SELECTION.md` 实测记录。
  - 测试覆盖：规则识别正反例、过滤/降权三路一致、增量更新幂等。
- 依赖：无（与 H-T1 可并行，但建议先做 H-T1 以便复测时确认语义模式在线）。

### [ ] H-T3（可选）教材问答冒烟脚本固化

- 背景：最近一轮联调的手工冒烟（教材问题 → 检索命中 → 带引用回答）验证
  价值很高，但不可重复。细节清单 D6-T8 是前端 Playwright E2E，与本项
  （后端真实链路冒烟）不同层面。
- 范围：`backend/scripts/` 新增脚本。
- 验收标准：临时目录数据库、真实 DeepSeek（读根目录 `.env`）跑一轮教材
  问答，断言 references 非空、检索事件出现、回答非空；输出脱敏（不打印
  凭证与消息正文全文）；失败退出码非零。可在任意间隙执行。

---

## Sprint D1–D7：阶段三细节清单（主体工作）

执行 `docs/TASKS_STAGE_3_DETAILS.md`。要点提示：

- 顺序按其依赖速览（第十一节）：D1（SSE 流式）→ D2（协作可视化/审批完整
  交互）→ D3（渲染增强）→ D4（会话体验）→ D5（设计系统/可访问性）→
  D6（反馈/知识库面板/E2E）→ D7（文件上传/多模态）。
- **D6-T3 有既有完成备注**（该文件 727 行起）：KnowledgeService 装配与
  `search_knowledge` 注入已完成，本次只做检索端点本身，不要重复装配。
- 每个任务完成后勾选该文件对应项；M3 出口检查（第十节）全过后，按其
  同步约定更新 `docs/TASK_BREAKDOWN_v2.md` 阶段三各项。
- 前端类型只从 OpenAPI 重新生成，不手写重复定义。

## Sprint 5：阶段一二收尾项（可选填充）

执行 `docs/TASKS_STAGE_1_2.md` 第 620 行起的 S5-T1～S5-T9（教案生成、
水平建模/错题分析、进度/审计报表、代码沙箱、LaTeX 渲染、学习记录工具、
工具动态加载、摘要压缩、PostgreSQL checkpointer）。全部为可选深化项，
优先级低于 H 与 D 系列，可插入任意间隙，门禁与执行规则相同。

---

## 出口检查

- [ ] H-T1 / H-T2 完成：新部署语义检索可启用、可诊断；向量噪音实测下降。
- [ ] 细节清单 D1–D7 全部勾选，M3 出口检查（其第十节）全部通过。
- [ ] 后端三项门禁 + 前端三项门禁全绿（全程不退化）。
- [ ] 真实 DeepSeek 冒烟：Web 界面完成一次带引用渲染的教材问答
      （流式可见、引用可点、评价事件可审计）。
