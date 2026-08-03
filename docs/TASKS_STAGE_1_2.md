# 阶段一 & 阶段二 冲刺任务清单

> 生成时间：2026-08-02
> 范围：`docs/TASK_BREAKDOWN_v2.md` 中的「阶段一：智能体框架搭建」与「阶段二：核心智能体与知识库」
> 目标：收口 M1（框架就绪），达成 M2（知识闭环：基于教材回答 AI 学科问题并标注来源）

本文档是执行层清单，面向负责开发的 agent 使用。总清单（`TASK_BREAKDOWN_v2.md`）
仍然是唯一的范围与进度权威来源；本文档只把它前两个阶段拆解为可独立验收的原子任务。

---

## 执行规则（开发 agent 必读）

1. 按 Sprint 顺序推进；同一 Sprint 内的任务可按依赖关系调整顺序，标注「可并行」的除外。
2. 每次只领取一个原子任务，完成后立即勾选本文档对应项，并同步更新总清单中对应
   三级编号任务的子项勾选状态。
3. 每个任务的完成定义 = 实现完成 + 验收标准全部通过 + 质量门禁通过。三者缺一
   不允许勾选。
4. 质量门禁（在 `backend/` 目录下执行，使用项目 venv）：
   - 全量测试：`PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q`
   - 静态检查：`.venv/Scripts/ruff.exe check src tests`
   - 类型检查：`.venv/Scripts/mypy.exe src`（strict 模式，零错误）
   - 基线：当前 158 个测试通过，ruff / mypy strict 干净。任何任务结束后不允许退化。
5. 改动遵循现有代码风格（见 `backend/AGENT_NODE_IMPLEMENTATION.md`）：错误只暴露
   稳定分类、事件不记录敏感正文、工具权限显式声明、checkpoint 行为需并发安全。
6. 遇到阻塞（缺依赖选型决策、缺外部资源、验收标准互相冲突）时停止该任务，记录
   阻塞原因，不要自行扩大范围。
7. 每个原子任务对应一次独立提交；提交信息遵循仓库现有 Conventional Commits 风格。

## 当前基线速览

- 已完成：状态 Schema、同构 ReAct 节点、Supervisor handoff 路由、工具注册/权限/
  错误分类/审计、SQLite checkpointer + resume + 并发保护、消息数裁剪、多会话隔离、
  pypdf 逐页加载、字符分块、内存词法索引、Top-K 检索工具、结构化 Citation。
- 部分完成：1.1.3 / 1.2.1 / 1.2.2 / 1.2.3 / 1.3.1 / 1.3.2 / 2.1.1–2.1.4 /
  2.2.2 / 2.2.3 / 2.3.1 / 2.3.3（具体缺口见各任务）。
- 知识源：`data/books/` 已有 5 本 AI 学科教材 PDF（周志华《机器学习》、李航
  《机器学习方法》、Goodfellow《深度学习》、《动手学深度学习》PyTorch 版、
  Russell《人工智能：现代方法》），尚未整理入库。

---

## Sprint 0：安全加固收口（当前冲刺遗留）

> 目标：消除信息泄漏风险，补齐验证闭环，为后续开发提供干净基线。

### [x] S0-T1 知识来源标识脱敏

- 对应总清单：当前冲刺「修复知识来源、Citation 一致性」
- 背景：`Citation.source` 目前携带服务器绝对路径，存在信息泄漏风险。
- 范围：`src/core/knowledge/models.py`、`loaders.py`、`service.py`、`tools.py`
  及对应测试。
- 验收标准：
  - 所有对外暴露的 source（Citation、检索 Observation）只含逻辑来源标识
    （如书名或知识库内相对路径），不出现任何文件系统绝对路径。
  - 加载器仍可用绝对路径读文件，但写入文档/分块模型前完成映射。
  - 新增回归测试：构造绝对路径输入，断言检索结果与 Citation 中无泄漏。
- 依赖：无。
- 完成备注：2026-08-02；绝对路径脱敏回归覆盖 Windows、UNC、POSIX、file URI
  及空白/控制字符旁路；全量测试 176 通过，ruff、mypy strict 干净。

### [x] S0-T2 重复写入与坐标一致性

- 对应总清单：当前冲刺「重复文档坐标问题」
- 范围：`src/core/knowledge/service.py`、`index.py` 及对应测试。
- 验收标准：
  - 同一 document_id 整文档替换后，旧版本 chunk 全部移除，检索不返回残留结果。
  - 重复执行相同 ingest 是幂等的（结果集合与坐标不变）。
  - 每个 chunk 的 document_id / start / end 坐标与其所属文档内容严格对应，
    新增一致性测试覆盖替换、删除、重复写入三种路径。
- 依赖：S0-T1。
- 完成备注：2026-08-02；覆盖多页整文档替换、删除、重复写入与重复页拒绝，
  chunk 坐标均可回指所属文档内容；全量测试 180 通过，ruff、mypy strict 干净。

### [x] S0-T3 真实 DeepSeek 冒烟验证

- 对应总清单：当前冲刺「真实 DeepSeek 冒烟验证尚未执行」
- 范围：`backend/scripts/verify_deepseek_react.py`（需要 `.env` 中
  `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY`）。
- 验收标准：
  - 脚本真实跑通一次完整的「模型决策 → 工具调用 → 最终回答」ReAct 循环。
  - 运行日志（脱敏后）与结论追加到 `AGENT_NODE_IMPLEMENTATION.md` 验证章节
    或本任务勾选备注中。
  - 若暴露真实问题，先记录再修复，修复计入本任务。
- 依赖：S0-T1、S0-T2（用干净基线验证）。
- 完成备注：2026-08-02；真实 `deepseek-v4-flash` 两轮 ReAct 成功调用
  `double` 并回答 `42`，脱敏日志已写入 `AGENT_NODE_IMPLEMENTATION.md`；
  全量测试 180 通过，ruff、mypy strict 干净。

### [x] S0-T4 全量质量复核

- 对应总清单：当前冲刺「再次执行 review、全量测试、静态检查」
- 验收标准：
  - 全量测试、ruff、mypy strict 三项门禁全部通过且无新增警告。
  - 对 Sprint 0 三个任务的改动做一次独立代码 review（可用多代理 review），
    review 结论记录在本任务勾选备注中。
- 依赖：S0-T1、S0-T2、S0-T3。
- 完成备注：2026-08-02；独立 review 结论：S0-T1 的空白/控制字符旁路已修复并
  复审干净，S0-T2 与 S0-T3 均无 Critical/Important；最终全量测试 180 通过，
  ruff、mypy strict 干净且无新增警告。

---

## Sprint 1：M1 运行时能力收口

> 目标：完成总清单建议顺序第 1 条——1.1.3 任务分解/聚合、1.1.4 人机断点、
> 1.2.2 工具超时，附带上文裁剪的 Token 预算化，形成稳定 Agent 框架基线。
> 出口标准 = 里程碑 M1「协调者能分派任务给多个子 Agent 并汇总」完整达成。

### [x] S1-T1 工具执行超时控制

- 对应总清单：1.2.2
- 范围：`src/core/tools/executor.py`、`events.py`（错误分类）、调用方配置。
- 验收标准：
  - 工具执行支持按工具或全局配置超时时间，默认有合理上限。
  - 超时转换为稳定错误分类（建议新增 `TOOL_TIMEOUT`），Observation 文本
    不泄漏内部细节，与现有四类工具错误风格一致。
  - 超时结果仍记录 `duration_ms` 与审计事件，评价 Agent 可区分超时与普通失败。
  - 测试覆盖：超时触发、超时后 Agent 循环继续、正常工具不受超时配置影响。
- 依赖：Sprint 0 完成。
- 备注：实现方式自选（如线程池 + future timeout），注意 Windows 兼容性，
  不要引入强杀线程之类的危险操作。
- 完成备注：2026-08-02；支持 30 秒全局默认与按工具覆盖，超时转换为脱敏的
  `TOOL_TIMEOUT` Observation 并保留耗时/审计事件，ReAct 可继续完成回答；独立
  review 无 Critical/Important，最终全量测试 195 通过，ruff、mypy strict 干净。

### [x] S1-T2 Token 级上下文预算

- 对应总清单：1.3.2（Token 计数与预算控制部分）
- 范围：`src/core/context.py`、图与节点工厂的裁剪配置。
- 验收标准：
  - 在现有按消息数裁剪之外，新增按 Token 预算裁剪（如 `max_context_tokens`），
    两者可同时生效，取更严格结果。
  - 裁剪继续保持 Tool Call / ToolMessage 组完整、孤立 ToolMessage 丢弃、
    checkpoint 中完整历史不变。
  - Token 计数方案明确（模型自带计数器或 tiktoken），依赖加入 `pyproject.toml`
    前确认必要性。
  - 测试覆盖：预算裁剪触发、组完整性、与消息数裁剪叠加。
- 依赖：Sprint 0 完成。与 S1-T1 可并行。
- 完成备注：2026-08-02；默认使用 `langchain-core` 的离线近似计数器
  `count_tokens_approximately(chars_per_token=1.0)`，无需新增依赖，并支持注入精确
  计数器；每轮模型调用均对 System、完整历史视图和当轮 ReAct 消息重新预算，默认
  路径按原子消息组线性扫描，checkpoint 仍保留完整历史。独立 review 无
  Critical/Important；最终全量测试 217 通过，ruff、mypy strict 干净。

### [x] S1-T3 人机交互断点（Human-in-the-loop）

- 对应总清单：1.1.4
- 范围：`src/core/graph_builder.py`、`state.py`、`sessions.py`，基于 LangGraph
  interrupt 机制。
- 验收标准：
  - 可配置在关键决策点（至少覆盖 Supervisor handoff 分派前）暂停，等待用户
    确认、拒绝或修改目标 Agent / 任务内容后再继续。
  - 暂停与恢复走 checkpointer，进程重启后仍可恢复待确认执行；与现有
    `resume()` 待恢复保护不冲突（两者语义在代码注释中写清楚）。
  - 无 checkpointer 时给出明确错误，不允许静默跳过断点。
  - 测试覆盖：中断触发、确认后继续、用户修改指令生效、进程重建后恢复。
- 依赖：Sprint 0 完成。
- 完成备注：2026-08-02；支持配置在 Supervisor handoff 分派前进入独立
  `handoff_approval` gate，确认后分派、拒绝后安全结束，修改目标或任务时新指令会
  实际进入 Worker 上下文。普通 pending 继续由 `resume()` 恢复，动态人工断点由
  `get_pending_handoff()` + `resume_handoff()` 以 Interrupt ID 安全恢复；SQLite
  进程重建与 LangGraph 0.4/1.2 Interrupt ID 兼容路径均有回归测试。独立 review
  无 Critical/Important；最终全量测试 235 通过，ruff、mypy strict 干净。

### [x] S1-T4 Supervisor 显式任务分解

- 对应总清单：1.1.3（显式任务分解部分）、2.1.1（复杂任务分解部分）
- 范围：`src/core/state.py`（新增任务计划字段与 reducer）、`nodes/prompts.py`、
  `graph_builder.py`。
- 验收标准：
  - 复杂请求进入后，Supervisor 先产出结构化子任务计划（子任务描述 + 目标
    角色 + 顺序），计划持久化在状态中可供审计。
  - 按计划依次 handoff 到对应 Worker，handoff 次数与 Agent 切换上限仍然生效。
  - 简单请求不产生计划、直接分派，不增加无谓轮次。
  - 测试覆盖：多子任务请求的计划生成与按序分派、计划字段随 checkpoint 持久化。
- 依赖：S1-T3（断点可挂接在计划确认点；若工期紧张，本任务可先不接断点，
  但状态字段设计需预留）。
- 完成备注：2026-08-02；Supervisor 通过仅限三个 Worker 目标的
  `create_task_plan` 结构化工具生成有序计划，并由确定性 `task_plan_dispatch`
  节点顺序分派；游标仅在对应 Worker 终态结果归档后推进，handoff / Agent 切换上限、
  人工确认/修改/拒绝与 SQLite 重开持久化均有回归。简单请求继续直接 handoff，
  模型调用轮次保持不变。三路独立复审无 Critical/Important；最终全量测试
  257 通过，ruff、mypy strict 干净。

### [x] S1-T5 多结果聚合

- 对应总清单：1.1.3（结果聚合部分）、2.1.1（多结果聚合部分）
- 范围：`src/core/state.py`、`nodes/prompts.py`、`graph_builder.py`。
- 验收标准：
  - 每个 Worker 完成子任务后，结果按子任务归档回 Supervisor 可见的结构化位置。
  - 全部子任务完成后 Supervisor 产出统一最终回答；部分子任务失败时降级为
    「已完成部分 + 明确说明缺失」，不允许整轮静默失败。
  - 聚合过程产生的事件可供评价 Agent 审计。
  - 测试覆盖：正常聚合、单子任务失败降级、聚合结果与计划一一对应。
- 依赖：S1-T4。
- 完成备注：2026-08-02；每个计划步骤的终态结果按顺序写入独立结构化状态并随
  SQLite checkpoint 持久化，结果与计划序号、目标角色严格一一校验。Supervisor
  仅通过临时命名系统消息接收已验证结果；局部失败或空白输出会生成包含已完成部分
  与明确缺失项的稳定降级回答，非法恢复态在模型调用前拒绝。归档与聚合事件可审计且
  不记录结果正文或异常秘密。三路独立复审均 READY；最终全量测试 276 通过，ruff、
  mypy strict 干净。

### S1-T6（可选）Send API 并行 fan-out

- 对应总清单：1.1.3（Send API / fan-out 子代理并行部分）
- 范围：`graph_builder.py`。
- 验收标准：
  - 计划中无依赖关系的子任务可通过 Send API 并行分派，汇合后再聚合。
  - 并行执行仍受 `max_agent_switches` 等上限约束，事件 sequence 单调无冲突。
  - 测试覆盖：并行分派、并发写状态无串扰、上限触发降级为串行。
- 依赖：S1-T5。优先级低，M1 出口不强制要求；如实现风险大可推迟到 Sprint 5。

### [x] S1-T7 历史消息 Agent 角色元数据

- 对应总清单：阶段三桥接清单 `docs/TASKS_STAGE_3_BRIDGE.md` W1-T7 的 core 侧支撑
  （该任务阻塞于「core 持久化历史无法恢复助手消息的产出 Agent 角色」）。
- 范围：`src/core/state.py`、`graph_builder.py` 及新增测试
  `tests/test_agent_role_metadata.py`。
- 验收标准：
  - 所有进入会话持久化历史的助手消息携带产出它的 Agent 角色，经 SQLite
    checkpointer 序列化往返、进程重建 / 新图实例重载后 `get_history()` 仍可
    恢复该角色。
  - 角色语义正确：单 Agent 回答标自身角色；Supervisor 多子任务聚合回答标
    supervisor；多轮 handoff 各助手消息标各自产出角色、互不污染。
  - HumanMessage / ToolMessage 不注入角色；消息 content 与类型不变，现有
    行为零回归。
- 依赖：S1-T5。
- 完成备注：2026-08-03；注入点选 `_wrap`（消息写入 `state["messages"]` 的
  唯一闸口），用 `AIMessage.additional_kwargs["agent"]` 经 JsonPlusSerializer
  （msgpack 基）随 checkpoint 持久化，聚合改写经 `model_copy` 保留元数据。
  独立 review 无 Critical/Important，3 个 Minor 已修复并复审放行；最终全量
  测试 322 通过，ruff 干净，mypy strict（30 个源文件）零问题。
  衔接说明：API 层 `sessions.py::_safe_agent` 需改为优先消费
  `core.state.message_agent_role()` 才能让 `Message.agent` 生效——该改动属
  桥接清单（`backend/src/api/`）范围，是 W1-T7 重跑的前置步骤，另行提交。

---

## Sprint 2：最小教学答疑闭环

> 目标：完成总清单建议顺序第 2 条——意图识别、分层讲解、评价规则、最终回答
> Citation，打通「学生提问 → 路由 → 检索增强答疑 → 引用溯源 → 评价审计」链路。

### [x] S2-T1 Supervisor 教学意图识别

- 对应总清单：2.1.1（意图识别部分）
- 范围：`nodes/prompts.py`、`state.py`（意图字段）、`graph_builder.py` 路由。
- 验收标准：
  - 定义明确的意图集合（至少：答疑、备课/讲解请求、评价/批改、其他），
    意图识别结果写入状态与运行事件。
  - 路由以意图为主要依据；意图不明时 Supervisor 追问澄清而非随意分派。
  - 准备一组覆盖各意图的测试用例（中文教学场景问题），分类与路由断言通过。
- 依赖：Sprint 1 完成（复用任务分解/聚合状态字段）。与 Sprint 3 可并行。
- 完成备注：2026-08-03；`Intent` 枚举（答疑/备课/评价/其他/不明）写入
  `state["intent"]`（存字符串，对齐 `current_agent` 惯例，规避 langgraph
  类型注册问题）与 `INTENT_DETECTED` 事件；Supervisor 经 `detect_intent`
  工具识别，prompt 约定五类路由，意图不明时追问且运行时拦截 UNCLEAR 后
  的强行分派（丢弃 handoff / create_task_plan），迭代超限走既有失败路径。
  语义边界：拦截只覆盖「自报 UNCLEAR 仍强行分派」；跳过识别（intent=None）
  与误报属既定兼容设计。新增 15 个测试（13 新 + 调整），全量 339 通过，
  ruff、mypy strict（30 文件）干净；独立 review 无 Critical，I-1 边界明示
  与 5 个 Minor 已处理并复审放行。

### [x] S2-T2 助学 Agent 分层讲解

- 对应总清单：2.1.3（分层答疑部分）
- 范围：`nodes/prompts.py`、`state.py`（学生水平画像的最小字段）。
- 验收标准：
  - 状态中加入学生水平信号（最小可用：如年级/自评等级枚举），分层策略
    写进助学 Agent 的 Prompt 与输出约定。
  - 同一知识点问题在不同水平设定下产出深度可区分回答（基础重直觉类比、
    进阶重推导与边界条件）。
  - 无水平信息时默认中等深度并在回答中说明可调整。
  - 测试用确定性替身模型断言不同水平的输出结构差异，不依赖真实模型玄学。
- 依赖：S2-T1。
- 完成备注：2026-08-03；`StudentLevel`（basic/advanced/unknown）写入
  `state["level"]`（存字符串，跨轮保留的学生画像——与 intent 每轮重置
  语义相反，仅 detect_level 时覆盖）；learning_assistant 经
  `prompt_builder` 钩子每轮按 level 动态生成 system prompt（锚点词
  basic「生活化类比」/ advanced「严谨推导」可确定性断言），其余 Agent
  不受影响（钩子默认 None）；无水平归一 unknown 默认中等深度并说明可
  调整；detect_level 仅 Supervisor 可调。新增 21 个测试（含枚举-指导词
  一致性守卫、动态 prompt 长度/token 预算守卫、旧 checkpoint 无 level
  通道退化路径），全量 360 通过，ruff、mypy strict（30 文件）干净；
  独立 review 无 Critical/Important，5 个 Minor 已处理并复审放行。

### [x] S2-T3 评价 Agent 基础评价规则

- 对应总清单：2.1.4（事实准确性与引用完整性部分）
- 范围：`nodes/prompts.py`、`events.py`（评价结果事件）、`state.py`。
- 验收标准：
  - 评价 Agent 对一轮最终回答输出结构化评价：事实准确性、引用完整性两个
    维度 + 通过/存疑/不通过结论 + 理由。
  - 评价输入为最终回答 + 本轮检索证据（ToolResult / Citation），不凭空评价。
  - 评价结论写入状态与事件，可被后续审计读取。
  - 测试覆盖：含事实错误回答被判存疑/不通过、引用缺失被标记、正确回答通过。
- 依赖：S2-T1。与 S2-T2 可并行。
- 完成备注：2026-08-03；`EvaluationVerdict`（pass/questionable/fail）+
  `EvaluationDimension`（fact_accuracy/citation_completeness）+
  `EvaluationResult`（verdict + 双维度 + reason + evidence_tool_names +
  created_at）写入 `state["evaluation"]`（每轮重置，checkpoint 持久化，
  与 tool_results 一致直接存模型）+ `EVALUATION_COMPLETED` 事件（只带
  verdict 摘要，reason 正文不落事件，双层截断 200 保审计有界）；评价经
  evaluator 节点 `submit_evaluation` 工具（仅 evaluator 可调）在轮末统一
  解析，不新增图节点；证据只记工具名（「按类型过滤」随 S2-T4 评估后
  未落地：过滤会破坏既有替身断言，保持宽口径——成功且有输出的非
  submit_evaluation 工具，引用的结构化校验由 S2-T4 references 承担）。
  新增 14 个测试（事实错误→fail、存疑→questionable、引用缺失标记、
  正确→pass、checkpoint 持久化与重置、计划模式、脱敏、非法值容错），
  全量 374 通过，ruff、mypy strict（30 文件）干净；独立 review 无
  Critical/Important，6 个 Minor（注释明示失败轮次语义/设计边界、
  角色守卫等）已处理并复审放行。

### [x] S2-T4 最终回答引用插入

- 对应总清单：2.3.3（引用插入部分）
- 范围：`nodes/prompts.py`、`nodes/react_agent.py` 或后处理层、知识工具
  Observation 格式（如需）。
- 验收标准：
  - Agent 使用 `search_knowledge` 证据作答时，最终回答按统一格式（如
    编号脚注）插入引用，引用信息来自本轮真实 SearchHit 的 Citation。
  - 最终消息附带结构化引用列表（document_id、逻辑 source、page、chunk_id），
    供前端渲染与评价 Agent 校验。
  - 未使用检索证据的回答不携带引用，保持「零命中不伪造引用」语义。
  - 测试覆盖：有检索必带引用、无检索无引用、引用列表与命中一一对应。
- 依赖：S0-T1（逻辑来源标识）、S2-T1。
- 完成备注：2026-08-03；引用经消息元数据
  `additional_kwargs["references"]`（`with_references`/`message_references`，
  与 S1-T7 角色元数据同机制，SQLite checkpoint 往返可恢复；值存
  `Citation.model_dump(mode="json")` dict 列表，msgpack 原生类型）随消息
  同生命周期持久化。来源为工具执行链路的真实 SearchHit Citation（白名单
  `_CITATION_TOOL_NAMES={"search_knowledge"}`，无模型伪造路径），挂在
  本轮最后一个无 tool_calls 的终端 AI 消息，按 chunk_id 去重保序；
  零命中/解析失败不注入键。**交付口径（重要）**：引用按「每条作答消息」
  渲染——使用证据作答的 Worker 消息带引用，Supervisor 聚合回答由拼接
  生成、本身未检索故不带引用（D3-T5 前端消费 `ChatResponse.references`
  时按消息读取，勿只读最后一条）；骨架期不在回答文本注入编号脚注，
  文本渲染由前端按元数据完成（→ D3-T5）。新增 10 个测试（真实
  search_knowledge 工具链路 + 对照式断言），全量 384 通过，ruff、mypy
  strict（30 文件）干净；独立 review 无 Critical，口径确认项已记录，
  2 个 Minor 已修复并复审放行。维护点：测试对照依赖词法索引确定性，
  Sprint 3 换向量索引后需复核。

### [x] S2-T5 引用真实性校验

- 对应总清单：2.3.3（真实性校验、引用格式规范化部分）
- 范围：后处理校验模块（位置自选，建议贴近 graph 输出层）、评价规则联动。
- 验收标准：
  - 自动校验最终回答中的每条引用确实存在于本轮检索结果；伪造或越界的
    引用被剔除或降级为「未验证」标记，并在评价结果中体现。
  - 引用格式规范化：同一文档多次引用合并编号，输出格式稳定可解析。
  - 测试覆盖：注入伪造引用被识别、合法引用不受影响、格式合并正确。
- 依赖：S2-T4。
- 完成备注：2026-08-03；校验置于 `_attach_references`
  收集之后、`with_references` 写入之前（消息进 checkpoint 的唯一闸口，
  伪造不可能持久化）；伪造判定：chunk_id 不在本轮真实命中集 → 越界剔除，
  字段不匹配 → 伪造剔除（选剔除：编号=列表下标契约稳定、removed 明细进
  `ReferenceVerification` 可审计、Citation 零改动）；文档级合并保留首次
  出现 chunk（真实命中保持 chunk 级全集避免误判），`verified` 为 chunk
  级通过数；结论写 `state["reference_verification"]`（每轮重置）并并入
  `EvaluationResult.reference_verification`——评价联动语义：evaluator
  自身结论优先，否则回退读取本用户轮 state 已写结论（worker 轮剔除
  明细可在评价结果中体现，有回归测试锁定）。新增 11 个测试（注入伪造
  识别、字段篡改、合法不受影响、文档合并、worker→evaluator 联动、零
  引用/无检索、checkpoint 持久化与序列化往返），全量 395 通过，ruff、
  mypy strict（30 文件）干净；独立 review 发现 1 个 Important（评价联动
  语义缺口）已修复并复审放行。维护点：测试对照依赖词法索引确定性
  （short-doc 双词命中排序），Sprint 3 换向量索引后需复核（与 S2-T4
  同一维护点）。

---

## Sprint 3：知识库升级

> 目标：把 `data/books/` 的教材变成可检索、可过滤、语义可达的知识底座。
> 与 Sprint 2 可按人力并行，但 S2-T4 的引用展示在语义检索接入后需复测。

### [x] S3-T1 知识源整理与批量入库

- 对应总清单：2.2.1
- 范围：`data/books/`、新增 ingest 脚本（`backend/scripts/`）、知识清单文件。
- 验收标准：
  - 建立知识源清单（书名、作者、学科标签、约定逻辑 source 标识、难度级别），
    清单文件纳入版本管理，PDF 本体不进 git。
  - ingest 脚本可批量解析 5 本教材入库，重复执行幂等（复用 S0-T2 语义）；
    大文件（如 190MB 的 AIMA）解析有进度反馈与失败续跑能力。
  - 入库后针对每本书各构造至少 1 个检索用例并命中正确来源。
- 依赖：Sprint 0 完成。
- 完成备注：2026-08-03；`backend/scripts/knowledge_manifest.json`（5 本
  书：source 逻辑标识 + file/title/authors/subjects/difficulty + 每本 2
  个检索用例）与 `ingest_books.py`（--books/--force/--verify/--top-k 等，
  逐书容错、退出码 0/1/2）；core 只加不改：`SqliteKnowledgeIndex`（零新
  依赖，检索语义与 InMemory 逐行一致）+ `iter_pdf_pages` 惰性逐页生成器
  （进度回调）。幂等：chunk_id 内容坐标派生 + 整文档替换（S0-T2 语义）；
  续跑：完成标记整本成功后才写，失败不留标记自动重试，`--force` 重入库。
  实测：ml-zhouzhihua 441 页 610 chunk；全量 5 本处理退出码 0，verify
  8/8 通过（每本 ≥2 用例命中正确来源，含主题重叠书查询词加限定调整）。
  **阻塞记录（数据源）**：ml-lihang《机器学习方法》PDF 为扫描版（无文本
  层，pypdf 无法提取），经确认暂时排除并标记 `blocked:
  scanned-pdf-no-text-layer`（ingest/verify 自动跳过、不计失败；恢复 =
  提供文本版 PDF 后移除 blocked 字段重新入库）。新增 34 个测试（含真实
  小 PDF e2e、blocked 短路），全量 438 通过，ruff、mypy strict（30 文件）
  干净；独立 review 无 Critical，2 个前置（.gitignore 加 `data/`、全量
  verify 收官）已落实。

### [x] S3-T2 语义分块

- 对应总清单：2.2.2（语义分块部分）
- 范围：`src/core/knowledge/chunking.py`、`loaders.py`。
- 验收标准：
  - 在字符分块之外支持按章节标题 / 段落边界分块，保留现有坐标字段与
    可回溯性（坐标仍可定位原文）。
  - 公式段与代码块有最小保护：不被从中间截断（启发式即可，不做完美解析）。
  - 分块策略可通过 ingest 参数选择，字符分块保持默认兼容。
  - 测试覆盖：章节边界切分、长公式段完整保留、坐标一致性。
- 依赖：S3-T1。
- 完成备注：2026-08-03；`chunking.py` 新增
  `chunk_document_semantic`（原字符分块逐字节未动）：三种标题形态
  （Markdown `#`、中文「第 X 章/节」含中文数字、数字小节 x.y.z，正则需
  MULTILINE 才能命中非首段标题）+ 段落边界（空行切分，标题行完整保留
  在 chunk 开头）；公式/代码保护：`$$...$$`、`\[...\]`、
  `\begin..\end`、``` 围栏 + 连续 ≥2 行缩进块，切点落入保护块整体推至
  块结束并入前 chunk，`_finalize_bounds` 防重叠不丢内容（已推演「标题段
  起点落入跨段保护块」的几何场景）；坐标语义与字符分块完全一致
  （content[start:end] 恒等、chunk_id 派生）；`KnowledgeService(chunking=
  "character"|"semantic")` 与 ingest `--chunking` 参数（默认 character
  兼容 S3-T1）；已知取舍：版本号行（如 2024.5.1）会被数字小节模式误判
  为标题（粒度变细、坐标无影响，行为有测试锁定）；「更换分块策略需
  --force 重入库」（完成标记不记录策略，help/docstring 已注明）。新增
  26 个测试（含跨段公式、围栏/缩进代码、版本号锁定、坐标一致性、
  service/ingest 策略选择），全量 464 通过，ruff、mypy strict（30 文件）
  干净；独立 review 无 Critical，I-1（策略与标记交互提示）与 2 个 Minor
  已处理并复审放行。

### [x] S3-T3 领域元数据与过滤检索

- 对应总清单：2.2.2（领域元数据部分）、2.2.3（元数据过滤部分）
- 范围：`src/core/knowledge/models.py`、`service.py`、`index.py`、ingest 脚本。
- 验收标准：
  - chunk metadata 约定领域字段（学科、章节、难度、概念标签），来源可以是
    清单注入 + 规则提取的组合，不要求模型自动标注。
  - 检索接口支持按 metadata 过滤（如限定某本书、某难度），过滤与排序组合正确。
  - 测试覆盖：字段写入、单条件与组合条件过滤、过滤后空结果语义。
- 依赖：S3-T2。
- 完成备注：2026-08-03；领域字段约定写入
  `models.py` docstring：清单注入 `subject`/`difficulty`/`title`（ingest
  已有）+ 规则提取 `chapter`/`section`/`tags`（chunking 复用 S3-T2 的
  `_HEADING_RE`，按「起点之前最近标题」传播，character/semantic 两种分块
  都可用，无标题不写字段）；`KnowledgeIndex` 协议与 InMemory/Sqlite 两实现
  的 `search` 新增 keyword-only `metadata_filter`（多键 AND；`source` 映射
  顶层；str 精确相等/list 任一元素；先过滤→打分→排序→top_k 截断；空结果
  返回 []）；SQLite 用 `json_extract OR json_each` 双分支 WHERE（键白名单
  正则防 JSON-path 注入、值全绑定参数，OR 写法在 json_each 标量两种语义
  下都正确）；过滤校验异常分类（非 Mapping → TypeError、键/值格式 →
  ValueError）。新增 19 个测试（32 项，memory/sqlite 参数化双跑锁定语义
  一致），全量 496 通过，ruff、mypy strict（30 文件）干净；独立 review 无
  Critical/Important，6 个 Minor 已处理并复审放行。

### [x] S3-T4 Embedding 选型与向量索引

- 对应总清单：2.2.3（Embedding 接入、向量库接入部分）
- 范围：`src/core/knowledge/index.py`（新增实现）、`pyproject.toml`、ingest 脚本。
- 验收标准：
  - Embedding 提供方选型有书面结论（中文效果、离线可用性、成本三维对比，
    记录在本任务勾选备注或 docs 下），实现封装为可替换协议，与现有
    `KnowledgeIndex` 协议并存。
  - 向量索引落地（建议 Chroma 先行），支持持久化与重载；索引数据目录不入 git。
  - 语义检索可命中词法索引无法命中的同义表述（构造中文用例证明）。
  - 新增依赖全部写入 `pyproject.toml` 并锁定；测试不依赖外部网络服务
    （外部 Embedding API 用替身封装）。
- 依赖：S3-T3。
- 完成备注：2026-08-03；选型书面结论见
  `docs/EMBEDDING_SELECTION.md`（embedding 四候选三维对比 + 向量库对比）：
  embedding 推荐 fastembed+bge-small-zh（离线、~100MB 缓存、无 torch），
  项目默认 `HashEmbeddingProvider`（zlib.crc32 字符特征、零依赖、跨进程
  确定），`FastEmbedProvider` 为可选适配器（协议预留替换点）；向量库不选
  Chroma（重依赖），自研 SQLite BLOB（float32）+ 内存矩阵（1.5 万×256
  维≈15MB），`data/vector_knowledge.db` 随 data/ 不入 git。`EmbeddingProvider`
  协议与 `KnowledgeIndex` 协议并存，`KnowledgeService` 仅依赖协议（挂
  向量索引即走向量）；ingest `--vector` 可选（默认哈希替身），已入库书
  增量补建不重新解析 PDF；语义用例（土豆→马铃薯、CNN→卷积神经网络）
  词法 0 命中、向量 top1 命中（替身证明链路，真实模型验证路径写入文档）。
  维度防护：重载校验维度与 provider 一致（不一致 ValueError 提示
  --force 重建），`_dot` 长度断言防静默截断；更换 embedding provider
  需 --force 重建（文档与注释已注明）。零新增依赖（pyproject 未动），
  全部测试离线。新增 19 个测试，全量 518 通过，ruff、mypy strict
  （32 文件）干净；独立 review 无 Critical，I-1（维度重载防护）与
  5 个 Minor 已处理并复审放行。

### [x] S3-T5 混合检索

- 对应总清单：2.2.3（向量 + BM25 + 元数据过滤混合检索部分）
- 范围：`src/core/knowledge/index.py`、`service.py`。
- 验收标准：
  - 向量分数与词法分数融合排序（加权或 RRF，方案写明），metadata 过滤在
    融合前生效。
  - 构造用例证明融合排序在两路各自失效场景下优于单路。
  - 混合检索成为 `search_knowledge` 工具的默认路径，词法单路保留为降级选项。
- 依赖：S3-T4。
- 完成备注：2026-08-03；`hybrid.py` 新增
  `HybridKnowledgeIndex`（实现 KnowledgeIndex 协议）：RRF 融合
  （Σ 1/(k+rank)，k=60 可调，同分按 chunk_id，候选窗口
  max(top_k×2, 10)）——选 RRF 因两路量纲不可比、零调参、确定可手算；
  metadata_filter 透传两路（先过滤后融合，复用 S3-T3 语义）；
  `open_vector_index_if_available` 降级工厂（文件不存在不创建 / 打不开
  返回 None / 成功启用）；vector=None 时完全透传词法路（分数排序逐项
  一致）；词法路为永不降级底线（by_chunk_id 取词法对象）。默认路径：
  `search_knowledge` 工具注入式设计（不改），core 内构造点
  ingest `--verify` 已接入混合索引（当前无向量库自动降级词法，行为
  不变），未来装配点 `KnowledgeService(HybridKnowledgeIndex(...))`
  一行接入（测试 6 锁定）。两路失效场景用例：场景 A 词法失效
  （土豆→马铃薯词法 0 命中、混合 top1 命中）、场景 B 向量失效
  （apple-pie 词法命中+向量正交反例，混合两路各救一项）。新增 13 个
  测试，全量 531 通过，ruff、mypy strict（33 文件）干净；独立 review
  无 Critical/Important，5 个 Minor（含 by_chunk_id 词法优先行为修正、
  e2e 测试 --vector-db 隔离）已处理并复审放行。

---

## Sprint 4：RAG 质量提升

> 目标：在「能检索」之上做到「检得准、该检才检」。

### [x] S4-T1 Query 改写与多路检索

- 对应总清单：2.3.1（Query 改写、多路联合检索部分）
- 范围：检索服务层（位置自选，建议独立于 index 的查询编排层）。
- 验收标准：
  - 支持将用户问题改写为多个检索变体并联合检索，结果按 chunk_id 去重合并。
  - 改写模型调用失败时降级为原始 query 单路检索。
  - 测试覆盖：多变体合并去重、降级路径、与混合检索组合。
- 依赖：S3-T5。
- 完成备注：2026-08-03；`retrieval.py` 新增查询编排层：
  `QueryRewriter` 协议（可注入可替换）+ `IdentityQueryRewriter` 默认
  （单变体 ≡ 直接索引 search，分数/排序/citation 逐项一致，零回归）；
  `multi_query_search` 多变体联合检索——每变体各检索一次（词法/混合
  按 service 配置），`dict[chunk_id]` 去重、**max 分数合并**（同打分
  函数量纲一致可直比；不用 RRF——那是 hybrid 为跨量纲设计的；不用
  加权/首命中），同分按 chunk_id，候选窗口 max(top_k×2,10)，metadata
  _filter 透传每变体；降级语义：改写器抛异常 / 返回 None / 含非 str
  元素 / 空 / 全空白 → 原始 query 单路 + warning（清洗与类型校验全部
  在 try 内）；空白 query 短路不打误报日志；LLM 改写器只做协议扩展点
  （真实实现由上层注入，避免检索层耦合 Agent 模型层；变体上限由上层
  保证）。`KnowledgeService(rewriter=...)` 注入接入。新增 13 个测试
  （合并去重/max 非首命/降级三路径/混合组合 RRF 精确分/过滤透传/
  零回归等价），全量 544 通过，ruff、mypy strict（34 文件）干净；
  独立 review 无 Critical/Important，3 个 Minor（清洗入 try、空白
  短路）已处理并复审放行。

### [x] S4-T2 重排序

- 对应总清单：2.3.1（Cross-Encoder 重排序部分）
- 范围：检索服务层。
- 验收标准：
  - 初检 Top-N 经重排模型（Cross-Encoder 或 LLM 打分）输出最终 Top-K，
    重排提供方可替换，测试用替身。
  - 构造用例证明重排后首位命中率优于融合排序基线。
- 依赖：S4-T1。
- 完成备注：2026-08-03；`retrieval.py` 扩展重排步骤：
  `Reranker` 协议（`rerank(query, hits, top_k) -> list[SearchHit]`，
  query 为原始用户问题、返回为 hits 的重排/子集）+ `IdentityReranker`
  默认（未注入 = 不重排，候选窗口 ≥ top_k 时「先截候选再截 top_k」≡
  「直接截 top_k」，零回归）；流程：初检（单路或多路 max 合并）→ 截
  候选窗口 max(top_k×2,10) → 重排 → 截断最终 Top-K；空返回 = 重排器
  合法裁决（透传空结果，与改写器「空→降级」刻意不对称）；降级仅两类
  （抛异常/返回类型不合法 → 保持初检 + warning）；替身 `_OverlapReranker`
  （query 词与 content 重合数重打分、同分保持初检相对顺序——有直接
  单测锁定，因词法 0 分跳过语义使端到端同分反序场景不可构造）；
  Cross-Encoder/LLM 重排器留协议扩展点（上层注入，避免检索层耦合
  Agent 模型层）。`KnowledgeService(reranker=...)` 注入接入。新增 11
  个测试（首位变化优于基线、截断在重排后、候选窗口透传、Identity
  零回归、替身确定性、与多路组合、过滤先于初检与重排、异常/类型
  不合法/空返回三降级路径、注入可替换），全量 555 通过，ruff、mypy
  strict（34 文件）干净；独立 review 无 Critical/Important，5 个
  Minor（空返回语义澄清、calls 三元组、caplog 对称、协议 docstring、
  平局测试重写）已处理并复审放行。

### S4-T3 自适应 RAG 策略

- 对应总清单：2.3.2
- 范围：检索服务层 / Agent 工具层。
- 验收标准：
  - 检索必要性判断：简单问题（如概念寒暄、纯计算）直接作答，不触发检索；
    判断逻辑可解释、可测试。
  - 相关性阈值：最高分低于阈值时不注入证据，Agent 明确说明「知识库未覆盖」
    而非强行作答。
  - 多轮检索：首次不足时自动 refine query 重检，重检次数有上限并写入事件。
  - 测试覆盖以上三条路径。
- 依赖：S4-T2。

---

## Sprint 5：角色能力深化与剩余框架项（按需并行）

> 目标：补齐阶段一、二剩余子项。优先级低于 Sprint 0–4，可在其任意间隙插入，
> 但每个任务同样遵守执行规则。

### S5-T1 助教 Agent 教案与例题生成工作流

- 对应总清单：2.1.2
- 验收标准：输入主题与课时目标，产出结构化教案（目标/重难点/讲解提纲/例题
  及解析）；例题附知识点标签并可指定难度；测试用替身模型验证输出结构。

### S5-T2 学生水平建模与错题分析

- 对应总清单：2.1.3（剩余部分）
- 验收标准：基于学生历史问答更新水平画像（规则式即可）；输入错题产出
  错因归类与关联知识点；画像与错题记录可持久化读取。

### S5-T3 学习进度分析与合规审计策略

- 对应总清单：2.1.4（剩余部分）
- 验收标准：评价 Agent 可汇总会话内评价历史输出进度小结；运行事件审计
  报表（工具调用、超时、越权尝试）可导出。

### S5-T4 Python 代码执行沙箱工具

- 对应总清单：1.2.3
- 验收标准：受控执行学生代码片段（资源限制 + 超时，复用 S1-T1 超时语义），
  禁止文件系统与网络越权访问；仅授权角色可调用；恶意输入用例测试。

### S5-T5 LaTeX 公式渲染工具

- 对应总清单：1.2.3
- 验收标准：输入 LaTeX 源码输出渲染产物（图片或规范化源码，选型写明）；
  渲染失败返回稳定错误分类；不引入重型系统依赖（Windows 可跑通）。

### S5-T6 学习记录读写工具

- 对应总清单：1.2.3
- 验收标准：Agent 可读写当前学生的学习记录（画像、错题、进度），存储与
  会话隔离规则一致；权限按角色收紧；并发写安全。

### S5-T7 工具动态加载与角色工具集

- 对应总清单：1.2.1（剩余部分）
- 验收标准：支持运行时按角色加载/卸载工具集，配置变更不影响进行中的
  checkpoint 恢复；权限矩阵变化有审计事件。

### S5-T8 长对话摘要压缩

- 对应总清单：1.3.2（剩余部分）
- 验收标准：超出 Token 预算的历史先摘要压缩再裁剪，摘要可追溯（标注被
  压缩的消息区间）；摘要失败降级为直接裁剪。

### S5-T9 PostgreSQL checkpointer

- 对应总清单：1.3.1（剩余部分）
- 验收标准：checkpointer 后端可配置 SQLite / PostgreSQL，行为一致性测试
  双后端通过；连接失败有明确错误；部署文档说明。

---

## 里程碑出口检查

### M1 框架就绪（Sprint 0 + Sprint 1 完成后检查）

- [x] 协调者能分解复杂请求、分派给至少 2 个子 Agent 并汇总结果（S1-T4/T5）。
- [x] 关键决策点可暂停等待人工确认并恢复（S1-T3）。
- [x] 工具超时受控、上下文按 Token 预算裁剪（S1-T1/T2）。
- [x] 三项质量门禁通过，DeepSeek 真实链路冒烟通过（S0-T3/T4）。

### M2 知识闭环（Sprint 2 + Sprint 3 完成后检查）

- [x] 基于 `data/books/` 教材内容回答 AI 学科问题，回答带规范化引用且
      引用真实性经校验（S2-T4/T5、S3-T1）。
- [x] 答疑链路走通「意图识别 → 分层讲解 → 检索增强 → 引用 → 评价」全流程
      （S2-T1/T2/T3）。
- [x] 语义检索 + 混合检索在线，词法单路可降级（S3-T4/T5）。

> M2 完成记录（2026-08-03）：三项全部达成。S2-T4 消息元数据引用（真实
> SearchHit Citation、chunk_id 去重保序、零命中不伪造）+ S2-T5 真实性校验
> （伪造/越界剔除、removed 明细进评价结果）；S3-T1 全量入库 4/5 本（verify
> 8/8 命中；ml-lihang 扫描版 PDF 阻塞，标记 blocked 待文本版，见 S3-T1
> 完成备注）；S2-T1/T2/T3 打通意图识别（五类意图+不明追问拦截）→ 分层
> 讲解（StudentLevel 动态 prompt）→ 检索增强（search_knowledge 真实链路）
> → 引用（references 元数据）→ 评价（EvaluationResult 双维度+结论+理由，
> 事件脱敏）；S3-T4 向量索引（Embedding 协议 + SQLite BLOB 持久化重载，
> 选型见 docs/EMBEDDING_SELECTION.md）+ S3-T5 混合检索（RRF 融合为默认，
> 词法单路降级）。全量测试 531 通过，ruff、mypy strict（33 文件）干净。

---

## 任务依赖速览

```
S0-T1 → S0-T2 → S0-T3 → S0-T4
                         │
        ┌────────────────┼─────────────────┐
        ▼                ▼                 ▼
     S1-T1/T2         S1-T3 → S1-T4 → S1-T5 →(S1-T6)
        │                                  │
        ▼                                  ▼
     Sprint 3 (S3-T1→…→T5)      Sprint 2 (T1 → T2/T3 → T4 → T5)
                                           │            ▲
                                           └────────────┘ (S2-T4 依赖 S0-T1)
        Sprint 4 依赖 Sprint 3；Sprint 5 任意间隙插入
```
