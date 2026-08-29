# 教案制作工作流设计 — lesson-workflow-design

> 状态：**设计稿 v0.1（待评审）** 2026-08-29
> 背景：长任务（备课）在 tool 模式嵌套 ask 路径下因「子代理无状态 + 迭代预算 + 审批暂停失效」三重约束必然失败（根因分析见会话记录 2026-08-29，关键证据 `backend/src/core/graph_builder.py:997` `_run_subagent`、`react_agent.py:471` 迭代上限）。
> 需求决策（已锁定）：意图自动触发；首期仅教案工作流；工作流中仅审批可交互、其余输入排队；写操作采用**产物区自动授权**。

---

## 一、目标与非目标

**目标**
1. 长任务从「嵌套 ask」切换到「图节点 Worker + 确定性调度」路径，解决失忆/预算/审批三个结构性问题。
2. 教案工作流全程零审批打断（产物区内自动授权），产出 docx 教案（六段教学设计模板 + 课标对齐 + 知识库引用）。
3. 工作流框架可注册化：PPT 等后续工作流按同一抽象挂载，上层（图入口/API）调用方式一致。
4. 短任务（答疑/寒暄/简单检索）嵌套 ask 路径零回归。

**非目标**
- 运行时动态生成工作流；工作流模板管理界面；handoff 兼容模式改造；PPT 工作流实现（仅预留接口）。

## 二、总体架构：混合编排

生产图保持 tool 模式编译，新增一条**工作流图路径**，按意图分流：

```
用户消息 → Supervisor 轮（detect_intent 识别 lesson_prep）
         → Supervisor 调用 start_workflow(workflow_id, params) 工具
             （模型只负责确认意图与抽取参数：课题/年级/课时等）
         → 轮末 _route 检测 state.workflow.status == RUNNING
         → 进入 _workflow_dispatch 调度节点（确定性，不走 ask）
         → Worker 图节点逐步执行（messages 经 add_messages 累积）
         → 全部步骤完成 → 回 Supervisor 收口轮（整合说明 + 下载回执）
```

设计要点：
- **触发**：意图识别与参数抽取沿用既有链路（`prompts.py` 角色卡 + `detect_intent`），新增一个 Supervisor 专属工具 `start_workflow`（仅注册教案工作流 id，参数 schema 约束），模型无法自造步骤顺序——顺序在代码定义的 `WorkflowDefinition` 里。
- **调度**：新节点 `_workflow_dispatch`，形态参照 handoff 模式的 `_dispatch_task_plan`（`graph_builder.py:2187`）：按 `workflow.current_step_index` 选 Worker、注入步骤指令（HumanMessage）、推进游标、写 `AGENT_SWITCHED` 事件。**不**复用 `_TOOL_PLAN_EXECUTION` 门控（工作流不走 ask，无乱序问题）。
- **执行**：Worker 复用 `_wrap` 包装为图节点（事件/引用/批改通道全部沿用），messages 写共享 state——跨步骤工作记忆由此获得。
- **收口**：`workflow.status` 置 COMPLETED 后路由回 Supervisor，其上下文已含全部步骤产物（messages 累积），输出最终说明；产物经既有下载回执链交付。

## 三、数据结构（state.py 新增）

```python
class WorkflowStepState(TypedDict):
    step_id: str
    worker_role: str            # teaching_assistant / evaluator / ...
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    attempts: int               # 含重试
    summary: str | None         # 步骤产出摘要（回传/展示用）

class WorkflowState(TypedDict):
    workflow_id: str            # "lesson_plan"
    status: Literal["running", "paused_approval", "completed",
                    "failed", "cancelled"]
    current_step_index: int
    steps: list[WorkflowStepState]
    artifact_root: str | None   # 产物区绝对路径（授权边界，见 §五）
    artifacts: list[str]        # 已登记产物文件（相对 artifact_root）
    budget: dict[str, int]      # model_calls / tool_calls 已用计数
    error_code: str | None
```

挂载为 `AgentState.workflow: WorkflowState | None`（`Annotated[..., overwrite_reducer]`，随 checkpoint 持久化）。`AgentState.messages` 已是 `add_messages` 追加语义（`state.py:936`），无需改动。

`WorkflowDefinition` / `WorkflowStep`（代码化静态定义，注册表模式）放 `core/workflows/` 新包：步骤的 worker_role、指令模板（六段教学设计模板在此）、成功判据、失败策略（沿用 TaskPlanStep 的 continue/abort/retry 语义 + 每步重试预算 1）、每步模型轮预算。

## 四、教案工作流状态机（首期）

| # | step_id | Worker | 职责 | 模型轮预算 | 失败策略 |
|---|---------|--------|------|-----------|---------|
| 1 | collect | teaching_assistant | 多路检索知识库（difficulty/source 过滤 + 课标对齐素材），整理成结构化素材稿 | 8 | abort |
| 2 | draft | teaching_assistant | 按六段教学设计模板成稿（基于步骤 1 累积上下文，不重新检索） | 4 | retry(1) |
| 3 | generate | teaching_assistant | `officecli_edit create` 生成 docx 到产物区（自动授权，见 §五），登记产物 | 4 | retry(1) |
| 4 | review | evaluator | 对照模板/课标清单校验产物结构（引用校验链沿用），不合格提出修订点 | 4 | continue |
| 5 | finalize | （回到 Supervisor） | 整合说明 + 产物回执；review 有修订点且有重试余量时可回退步骤 2-3 一次 | — | — |

- 每步是一个独立图步（checkpoint 边界）：刷新/重启后从 `current_step_index` 恢复。
- 步骤 1-2 之间靠 messages 累积传递工作成果——**这是对失忆根因的直接修正**。
- 审批暂停路径保留：任何步骤内若触发产物区**外**写操作 → `pending_tool_approval` → 顶层 `_approve_tool` 节点暂停（`workflow.status=paused_approval`）→ 批准后从断点恢复。首期教案流预期零审批。

## 五、产物区自动授权（C1 定案落地）

- **路径规则**：产物区 = 会话工作区内 `.workflow-artifacts/<run_id>/`（工作流启动时创建并写入 `WorkflowState.artifact_root`）。放在工作区**内**是为完整复用既有路径逃逸防护与授权根解析，不新开边界。
- **授权判定**：`ToolExecutor` 的审批决策点增加一个先置检查——officecli_edit 类写工具的目标文件参数，经既有绝对路径重写解析后若落在 `artifact_root` 前缀内 → 跳过审批直接执行（动词白名单对该路径同步放宽 remove/batch，因为产物即本运行创建）；产物区外一切照旧。
- **审计可见**：自动放行在 `TOOL_COMPLETED` 事件上带 `auto_approved=True` 标记（前端显示"产物区自动授权"），事件协议不变、消费方安全跳过。
- **红线不变**：`shell` 不参与自动授权；既有文件（含用户上传）的读不受影响、写/删必须审批；密钥脱敏、工作区逃逸防护原样。

## 六、输入排队（B2 定案落地）

- API 层（`api/chat.py` / `stream.py`）：收到新消息时若 `state.workflow.status in {running, paused_approval}` 且非审批决策请求 → 写入会话 `queued_messages`（SessionStore 新字段，落 SQLite），立即返回排队回执（SSE `WORKFLOW_INPUT_QUEUED` 事件 + ChatResponse 标记），**不**进入图。
- 工作流收口后，Supervisor 收口轮的提示词尾部附"排队输入清单"，由模型决定顺带回应或提示用户重新发起；排队记录随即清空。
- 审批决策请求不受排队影响（走既有审批通道）。

## 七、事件与契约

新增 EventType（沿用既有脱敏原则，只记枚举/计数不记正文）：
`WORKFLOW_STARTED / WORKFLOW_STEP_STARTED / WORKFLOW_STEP_COMPLETED / WORKFLOW_STEP_RETRY / WORKFLOW_COMPLETED / WORKFLOW_FAILED / WORKFLOW_INPUT_QUEUED`。
`api/chat.py` 的 `EVENT_TYPE_MAP` 白名单补映射；未映射事件安全跳过的机制保证灰度期新旧前端兼容（`events.py:26-29` 先例）。

契约：`ChatResponse` / `SessionProcess` 各新增 `workflow: WorkflowDto | None`（形状对齐 §三，参照 `task_plan: TaskPlanDto` 先例，`schemas.py:750/760`）；`openapi.json` + `api.generated.ts` 重导同步（`scripts/export_openapi.py`）。

前端：`collaboration-panel` 增加工作流进度块（步骤 N/M、当前 Worker、每步状态图标、产物下载入口）；`chat-input` 排队态提示；`error-messages.ts` 增加 `workflow_budget_exceeded` 文案。

## 八、预算与错误

- 每步模型轮预算见 §四表；全局工具调用上限按步骤核算（初值：collect 12 / draft 6 / generate 8 / review 6），超限该步骤按失败策略处置。
- 新错误码 `WORKFLOW_BUDGET_EXCEEDED = "workflow_budget_exceeded"`（归入 ErrorCode 编排层分组），终局失败时计划态置 FAILED、发 `WORKFLOW_FAILED`。
- 既有 `max_handoffs/max_agent_switches` 对工作流路径改为按 `steps × (1 + retry)` 核算，不沿用嵌套 ask 时代的常数。

## 九、改动点清单（file-level）

| 层 | 文件 | 改动 |
|---|---|---|
| 状态 | `core/state.py` | WorkflowState/WorkflowStepState + AgentState.workflow |
| 事件 | `core/events.py` | WORKFLOW_* 事件类型 + ErrorCode.WORKFLOW_BUDGET_EXCEEDED |
| 工作流 | `core/workflows/`（新） | definition.py（注册表/步骤定义）、dispatch 逻辑纯函数 |
| 编排 | `core/graph_builder.py` | start_workflow 工具、_workflow_dispatch 节点、_route 工作流分支、收口路由 |
| 工具 | `core/tools/`（executor） | 产物区审批豁免通道 + auto_approved 标记 |
| 提示词 | `core/nodes/prompts.py` | Supervisor 工作流触发约定；六段模板步骤指令 |
| API | `api/chat.py` `stream.py` `sessions.py` `schemas.py` | 排队、workflow 字段、事件映射 |
| 契约 | `contracts/openapi.json` `api.generated.ts` | 重导 |
| 前端 | `collaboration-panel` `chat-input` `error-messages` | 进度块、排队态、错误文案 |
| 环境开关 | `.env.example` `api/app.py` | `API_WORKFLOW_MODE=off|auto`（默认 off，验收前切 auto；出问题可一键回退嵌套 ask） |

## 十、测试计划

1. 单测：状态机推进/失败策略/重试边界/取消；产物区授权（区内放行、区外审批、逃逸路径拒绝、边界前缀混淆）；预算超限；排队写入与清空。
2. 恢复测试：审批暂停→批准→断点续跑；checkpoint 重载后从 current_step_index 继续（沿用 `test_graph_persistence.py` 模式）。
3. 契约测试：openapi 同步、事件白名单映射、排队回执。
4. 前端：进度块/排队态组件测试（沿用 vitest 现有模式）。
5. 端到端：真实模型冒烟一条（"准备反向传播教案"全流程零审批产出 docx）；既有 1074 后端 + 327 前端测试零回归。

## 十一、依赖与风险

| 风险 | 缓解 |
|---|---|
| 课标 PDF 尚未入库（manifest blocked 条目） | 步骤 1 的课标对齐素材降级为模板内置约定（prompts 已有课标对齐卡）；入库后 `source=cs-ai-curriculum` 过滤自动生效，无需改工作流 |
| `_route` 是全图共用路径，改动回归面大 | 独立工作流分支 + `API_WORKFLOW_MODE` 开关；分支不命中时行为逐字节等价 |
| 步骤 2 依赖步骤 1 的长上下文 | messages 累积 + 既有上下文裁剪护栏（512K）兜底 |
| officecli create 目标路径冲突 | 产物目录按 run_id 隔离，重试沿用同一路径（officecli create 语义需在实现前核对：已存在时的行为决定 retry 策略细节） |

## 十二、里程碑

- **M1** 状态与事件基座：WorkflowState/事件类型/契约重导（后端可独立合入）
- **M2** 调度节点与图接线：start_workflow → dispatch → Worker 轮 → 收口（开关保护）
- **M3** 产物区授权 + docx 生成步骤 + 回执链
- **M4** 排队语义 + 前端进度块
- **M5** 测试补全 + 真实模型冒烟 + 文档（DOCS_AUTHORITY 登记、README 工作流章节）

## 十三、开放问题（实现前定夺即可）

1. 产物命名与展示：`教案-<课题>-<日期>.docx` 由步骤 3 的模型参数决定还是模板规则？建议模板规则 + 模型仅填课题变量。
2. 取消交互：首期是否提供前端"取消工作流"按钮（对应 CANCELLED），还是仅排队文本指令？建议首期按钮（成本小、演示可控性强）。
3. review 回退（步骤 5 重入 2-3）首期是否启用：建议启用但重试总量 1 次，超限按 FAILED 收口——避免无限循环复现旧问题。
