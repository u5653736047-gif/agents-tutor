"""全局状态 Schema 定义.

本模块定义 LangGraph StateGraph 的核心状态结构 `AgentState`，
作为多智能体协作的共享上下文，所有 Agent 节点通过读写该状态完成信息传递。

设计原则：
- 顶层使用 TypedDict 以兼容 LangGraph StateGraph 的状态通道机制
- 嵌套结构使用 Pydantic BaseModel 提供字段验证与序列化能力
- 通过 Annotated + reducer 函数控制并发写入时的合并策略
- 字段设计面向扩展：新增字段只需在 TypedDict 中追加即可
"""

from __future__ import annotations

import operator
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .events import ErrorCode, RunError, RunEvent

# ─────────────────────────────────────────────
# 枚举定义
# ─────────────────────────────────────────────


class AgentRole(StrEnum):
    """智能体角色标识.

    对应系统中的四类角色化 Agent，
    与 StateGraph 中注册的节点名称一一对应。
    """

    SUPERVISOR = "supervisor"
    TEACHING_ASSISTANT = "teaching_assistant"
    LEARNING_ASSISTANT = "learning_assistant"
    EVALUATOR = "evaluator"


class Intent(StrEnum):
    """教学场景的用户意图分类（S2-T1 意图识别层）。

    为什么需要这个枚举（对应验收标准「定义明确的意图集合」）：
    - Supervisor 的分派决策（直接回答 / handoff 到 Worker / create_task_plan
      分解）以意图为首要依据，枚举把「模型的自由文本判断」收敛为稳定、
      可校验、可审计的标签；
    - 枚举值（字符串形式，见 AgentState.intent 注释）会写入 state["intent"]
      （随 checkpoint 持久化）与
      INTENT_DETECTED 运行事件，前端与审计可据此回放「这一轮用户想干什么」；
    - 可扩展：新增意图只需在此追加枚举值，并在 nodes/prompts.py 的
      Supervisor 提示词中补充对应路由说明，不需要改图结构。

    各值含义与 Supervisor 的默认路由（详细约定见 prompts.py）：
    - ANSWER_QUESTION 答疑：学生提问求解答 → 直接回答，或转
      learning_assistant（助学 Agent）做深入辅导/学习规划；
    - LESSON_PREP 备课/讲解请求：生成教案/讲解材料 → teaching_assistant（助教）；
    - EVALUATION 评价/批改：作业评价/批改 → evaluator（评价 Agent）；
    - OTHER 其他：模型能确定但不在上述三类 → 直接回答；
    - UNCLEAR 意图不明：模型无法确定 → Supervisor 必须追问澄清，
      禁止 handoff 或 create_task_plan（graph_builder 有运行时兜底拦截）。
    """

    ANSWER_QUESTION = "answer_question"
    LESSON_PREP = "lesson_prep"
    EVALUATION = "evaluation"
    OTHER = "other"
    UNCLEAR = "unclear"


class StudentLevel(StrEnum):
    """学生水平画像分类（S2-T2 分层讲解）。

    为什么需要这个枚举（对应验收标准「至少 基础/进阶 两档 + 默认未知」）：
    - 助学 Agent（learning_assistant）的讲解深度按学生水平分层：基础
      BASIC 重直觉类比、进阶 ADVANCED 重推导与边界条件、未知 UNKNOWN
      默认中等深度并说明可调整；
    - 枚举把「学生自评/模型识别」的自由文本收敛为稳定、可校验的标签，
      与 Intent 一样写入 state["level"]（枚举值字符串，见 AgentState.level
      注释）与 task_context.level 快照；
    - 可扩展：新增档位只需在此追加枚举值，并在 nodes/prompts.py 的
      _LEVEL_GUIDANCE 中补充对应讲解策略，不需要改图结构。

    与 Intent 的关键差异（生命周期语义，这是 S2-T2 的核心设计）：
    - Intent 是「本轮意图」：每轮重新识别，run() 在新用户轮次重置；
    - StudentLevel 是「跨轮保留的学生画像」：新轮不重置，仅当模型再次
      调用 detect_level（学生自报新水平）时才覆盖；首次提问无水平信息
      时保持 None，读取侧（prompts.learning_assistant_system_prompt）
      按 UNKNOWN 归一处理（默认中等深度）。
    """

    BASIC = "basic"
    ADVANCED = "advanced"
    UNKNOWN = "unknown"


# ─────────────────────────────────────────────
# 助手消息的 Agent 角色元数据
# ─────────────────────────────────────────────

# 所有进入会话持久化历史的助手消息（AIMessage）都会在写入状态前，
# 于 additional_kwargs 中写入「产出该消息的 Agent 角色」。
#
# 为什么用 additional_kwargs 而不是消息的 name 字段：
# - additional_kwargs 是 LangChain 消息的标准附加字段，LangGraph 的
#   SQLite checkpointer 默认使用 JsonPlusSerializer（msgpack 基）序列化
#   消息时会原样保留该字段，因此进程重建、状态重载后 get_history()
#   读出的消息仍能恢复角色（这是验收核心，测试 test_agent_role_metadata
#   覆盖序列化往返）。
# - name 字段会被部分模型 API 当作说话人标识透传给模型，且现有代码
#   已用 name 标记 task_results 系统消息，占用它会引入语义混叠。
#
# 写入端严格（只写 AgentRole 的合法枚举值），读取端宽容（见
# message_agent_role）：宁可返回 None 也不让异常数据击穿前端。
AGENT_ROLE_METADATA_KEY = "agent"


def with_agent_role(message: AIMessage, role: AgentRole) -> AIMessage:
    """返回携带产出 Agent 角色的 AIMessage 副本（不修改原对象）。

    为什么返回副本而非就地修改：
    - 模型返回的 AIMessage 对象可能被调用方复用（如再次作为模型输入），
      就地修改会污染模型看到的历史；
    - model_copy 只替换 additional_kwargs，content、tool_calls、
      response_metadata 等字段原样保留，因此不会改变对外消息内容。
    既有 additional_kwargs（如模型返回的 provider 元数据）也会保留，
    只新增 AGENT_ROLE_METADATA_KEY 一个键。
    """
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs[AGENT_ROLE_METADATA_KEY] = role.value
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def message_agent_role(message: BaseMessage) -> AgentRole | None:
    """从消息元数据读出产出它的 Agent 角色；无法确定时返回 None。

    设计取舍：
    - HumanMessage / ToolMessage / SystemMessage 不注入角色，
      读出的 None 表示「该消息没有角色」而非数据错误；
    - 键存在但值非法（历史脏数据、未来枚举变更）时同样返回 None，
      保证 get_history() 的消费者（前端角色徽章）不会因异常崩溃。
    """
    raw = message.additional_kwargs.get(AGENT_ROLE_METADATA_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return AgentRole(raw)
    except ValueError:
        return None


WorkerAgentRole = Literal[
    AgentRole.TEACHING_ASSISTANT,
    AgentRole.LEARNING_ASSISTANT,
    AgentRole.EVALUATOR,
]


class TaskStatus(StrEnum):
    """任务生命周期状态."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPlanStatus(StrEnum):
    """Supervisor 显式任务计划的调度状态。"""

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class HandoffApprovalAction(StrEnum):
    """人工对 Supervisor 分派提案的处理动作。"""

    CONFIRM = "confirm"
    REJECT = "reject"
    MODIFY = "modify"


# ─────────────────────────────────────────────
# Pydantic 子模型（嵌套结构）
# ─────────────────────────────────────────────


class TaskContext(BaseModel):
    """当前任务的结构化上下文.

    由 Supervisor 在任务分解阶段填充，
    各子 Agent 读取自身相关的任务信息执行工作。
    """

    task_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    # 注意与 state["intent"] 的约束差异是有意的：
    # - state["intent"] 是严格校验后的 Intent 枚举值字符串（每轮重置，
    #   本轮意图的权威值，见 AgentState.intent 注释）；
    # - task_context.intent 是自由字符串的任务上下文快照（跨轮持久，
    #   供 Worker/聚合读取），保留宽松约束以便容纳历史数据与未来扩展。
    intent: str = Field(default="", description="用户意图分类标签（自由字符串快照）")
    # S2-T2 学生水平：与 state["level"] 的约束差异是有意的（同 intent 模式）：
    # - state["level"] 是严格校验后的 StudentLevel 枚举值字符串（跨轮保留的
    #   权威画像，见 AgentState.level 注释）；
    # - task_context.level 是自由字符串的任务上下文快照（随任务分派写入，
    #   供 Worker/聚合读取），保留宽松约束以便容纳历史数据与未来扩展。
    level: str = Field(default="", description="学生水平标签（自由字符串快照）")
    description: str = Field(default="", description="任务自然语言描述")
    subtasks: list[str] = Field(default_factory=list, description="分解后的子任务列表")
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展元数据（难度级别、学科标签、关联知识点等）",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TaskPlanStep(BaseModel):
    """一个按序执行、面向 Worker 的计划步骤。"""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    description: str = Field(min_length=1)
    target_agent: WorkerAgentRole

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, description: str) -> str:
        if not description.strip():
            raise ValueError("plan step description must not be blank")
        return description


class TaskPlan(BaseModel):
    """可持久化、可审计的 Supervisor 有序任务计划。"""

    model_config = ConfigDict(extra="forbid")

    steps: list[TaskPlanStep] = Field(min_length=2)
    current_step_index: int = Field(default=0, ge=0)
    status: TaskPlanStatus = TaskPlanStatus.ACTIVE

    @field_validator("steps")
    @classmethod
    def steps_must_have_contiguous_sequences(
        cls,
        steps: list[TaskPlanStep],
    ) -> list[TaskPlanStep]:
        ordered = sorted(steps, key=lambda step: step.sequence)
        if [step.sequence for step in ordered] != list(range(1, len(steps) + 1)):
            raise ValueError("plan step sequences must be contiguous from 1")
        return ordered

    @model_validator(mode="after")
    def progress_must_match_status(self) -> TaskPlan:
        step_count = len(self.steps)
        if self.current_step_index > step_count:
            raise ValueError("current_step_index exceeds plan length")
        if (
            self.status is TaskPlanStatus.ACTIVE
            and self.current_step_index == step_count
        ):
            raise ValueError("completed plan cannot remain active")
        if (
            self.status is TaskPlanStatus.COMPLETED
            and self.current_step_index != step_count
        ):
            raise ValueError("completed plan must consume every step")
        return self


class TaskStepResult(BaseModel):
    """一个计划步骤的终态执行结果，不包含异常正文或工具参数。"""

    model_config = ConfigDict(extra="forbid")

    step_sequence: int = Field(ge=1)
    target_agent: WorkerAgentRole
    success: bool
    output: str | None = None
    error_code: ErrorCode | None = None

    @field_validator("output")
    @classmethod
    def output_must_not_be_blank(cls, output: str | None) -> str | None:
        if output is not None and not output.strip():
            raise ValueError("successful task result output must not be blank")
        return output

    @model_validator(mode="after")
    def outcome_fields_must_match_success(self) -> TaskStepResult:
        if self.success:
            if self.output is None or self.error_code is not None:
                raise ValueError("successful task result requires only output")
        elif self.output is not None or self.error_code is None:
            raise ValueError("failed task result requires only error_code")
        elif self.error_code not in {
            ErrorCode.MODEL_CALL_FAILED,
            ErrorCode.REACT_ITERATION_LIMIT,
            ErrorCode.AGENT_OUTPUT_INVALID,
        }:
            raise ValueError("task result error_code is not locally recoverable")
        return self


class HandoffApprovalRequest(BaseModel):
    """等待人工确认的 Supervisor 分派提案。"""

    model_config = ConfigDict(extra="forbid")

    target_agent: AgentRole
    task_content: str
    plan_step_sequence: int | None = Field(default=None, ge=1)

    @field_validator("target_agent")
    @classmethod
    def target_must_be_worker(cls, target: AgentRole) -> AgentRole:
        if target is AgentRole.SUPERVISOR:
            raise ValueError("handoff target must be a worker agent")
        return target


class HandoffApprovalDecision(BaseModel):
    """带中断标识的人工分派决定，防止陈旧确认误用。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1)
    action: HandoffApprovalAction
    target_agent: AgentRole | None = None
    task_content: str | None = None

    @field_validator("interrupt_id")
    @classmethod
    def interrupt_id_must_not_be_blank(cls, interrupt_id: str) -> str:
        if not interrupt_id.strip():
            raise ValueError("interrupt_id must not be blank")
        return interrupt_id

    @field_validator("target_agent")
    @classmethod
    def target_must_be_worker(cls, target: AgentRole | None) -> AgentRole | None:
        if target is AgentRole.SUPERVISOR:
            raise ValueError("handoff target must be a worker agent")
        return target

    @field_validator("task_content")
    @classmethod
    def task_content_must_not_be_blank(cls, task_content: str | None) -> str | None:
        if task_content is not None and not task_content.strip():
            raise ValueError("task_content must not be blank")
        return task_content

    @model_validator(mode="after")
    def action_matches_changes(self) -> HandoffApprovalDecision:
        has_changes = self.target_agent is not None or self.task_content is not None
        if self.action is HandoffApprovalAction.MODIFY and not has_changes:
            raise ValueError("modify requires target_agent or task_content")
        if self.action is not HandoffApprovalAction.MODIFY and has_changes:
            raise ValueError("only modify accepts target_agent or task_content")
        return self


class PendingHandoffApproval(BaseModel):
    """公开给调用方的待确认断点标识与分派提案。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1)
    request: HandoffApprovalRequest


class ToolResult(BaseModel):
    """单次工具调用的结构化结果.

    由工具执行器在工具运行完毕后写入状态，
    供后续 Agent 节点读取和评价 Agent 审计。
    """

    tool_call_id: str = Field(description="对应 LLM tool_call 的唯一 ID")
    tool_name: str = Field(description="被调用的工具名称")
    agent_role: AgentRole = Field(description="发起调用的 Agent 角色")
    success: bool = Field(default=True)
    output: str = Field(default="", description="工具返回的文本结果")
    error: str | None = Field(default=None, description="失败时的错误信息")
    error_code: ErrorCode | None = Field(default=None, description="失败错误分类")
    duration_ms: float = Field(default=0.0, description="执行耗时（毫秒）")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ─────────────────────────────────────────────
# Reducer 函数
# ─────────────────────────────────────────────


def _replace(existing: Any, new: Any) -> Any:
    """直接覆盖式 reducer；显式 None 也用于清空旧状态."""
    return new


# ─────────────────────────────────────────────
# 全局状态定义（LangGraph StateGraph 入口）
# ─────────────────────────────────────────────


class AgentState(TypedDict, total=False):
    """多智能体系统全局状态.

    基于 TypedDict 定义，与 LangGraph StateGraph 状态通道机制原生兼容。
    各字段通过 Annotated 指定 reducer 控制并发更新语义：
    - messages: 追加合并（由 langgraph add_messages 处理去重与按 ID 更新）
    - tool_results、events: 追加合并（operator.add 拼接列表）
    - task_context、extra: 后写覆盖，但作为跨轮持久字段保留
    - task_plan、task_results: 后写覆盖；新用户轮次清空，历史 checkpoint 保留
    - next_agent、pending_handoff、run_error、handoff_count、agent_switch_count:
      后写覆盖，每轮开始重置
    - 其余字段: 后写覆盖（last-write-wins）

    total=False 使所有字段变为可选，允许节点仅返回部分更新（partial update）。

    Usage::

        from langgraph.graph import StateGraph
        graph = StateGraph(AgentState)
    """

    # --- 对话历史 ---
    # add_messages reducer 支持追加、按 ID 更新、批量合并
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # --- Agent 调度信息 ---
    # 当前正在执行的 Agent 角色（last-write-wins）
    current_agent: Annotated[str | None, _replace]

    # 路由决策：Supervisor 输出的下一步目标节点名称
    next_agent: Annotated[str | None, _replace]

    # Supervisor 已提出、尚未由人工确认的分派；checkpoint 是唯一事实来源。
    pending_handoff: Annotated[HandoffApprovalRequest | None, _replace]

    # --- 意图识别（S2-T1） ---
    # Supervisor 本轮识别出的用户意图（Intent 枚举的 value 字符串）；
    # last-write-wins，由 detect_intent 工具结果经 _wrap 校验后写入，
    # run() 在新用户轮次重置为 None。
    #
    # 为什么存字符串而不是 Intent 枚举：
    # - checkpoint 会 msgpack 序列化 state 的全部通道，自定义枚举类型在
    #   反序列化时依赖 langgraph 的「类型注册表」（未注册类型当前仅警告、
    #   未来版本会阻断），存枚举值字符串则永远是 msgpack 原生类型，
    #   彻底消除该版本风险；
    # - 与既有惯例一致：current_agent 通道同样存 role.value 字符串而非
    #   AgentRole 枚举；读取方需要枚举时用 Intent(state["intent"]) 转换即可
    #   （StrEnum 与字符串的 == 比较天然成立，测试断言不受影响）。
    #
    # 为什么放在 state 而不是只发事件：
    # 1) checkpoint 持久化——事件只存在于当次运行的 events 列表中，跨轮不可查；
    #    state 字段随 checkpoint 保存，get_state()/恢复会话时仍能读到
    #    上一轮的意图快照，是「审计」的事实来源；
    # 2) 权威值——ToolResult 只记录「模型声称的意图」，state["intent"]
    #    是 _wrap 校验后的权威分类，两者对照可以发现模型谎报或乱填；
    # 3) 路由依据——Supervisor 分派（handoff / create_task_plan）时把意图
    #    同步进 task_context.intent，供 Worker 与后续聚合读取。
    # 意图不明（UNCLEAR）时的追问逻辑见 graph_builder._wrap 的拦截说明。
    intent: Annotated[str | None, _replace]

    # --- 学生水平画像（S2-T2） ---
    # 学生水平（StudentLevel 枚举的 value 字符串）；last-write-wins，
    # 由 detect_level 工具结果经 _wrap 校验后写入。
    #
    # 为什么存字符串而不是 StudentLevel 枚举：
    # 与 intent 同一理由——checkpoint 的 msgpack 序列化对自定义枚举有
    # 类型注册依赖（未注册类型当前仅警告、未来版本会阻断），存枚举值
    # 字符串永远是 msgpack 原生类型；读取方需要枚举时用
    # StudentLevel(state["level"]) 转换（StrEnum 与字符串的 == 比较
    # 天然成立，测试断言不受影响）。
    #
    # 与 intent 字段的异同（这是 S2-T2 的关键设计，务必区分）：
    # - 相同点：都写在 state 而非只发事件（随 checkpoint 持久化），
    #   都是「模型识别结果经 _wrap 校验后的权威值」，都存枚举值字符串；
    # - 不同点（重置 vs 保留）：intent 是「本轮意图」，run() 在新用户
    #   轮次重置为 None、每轮重新识别；level 是「跨轮保留的学生画像」，
    #   run() 的重置列表刻意不含 level——只有模型再次调用 detect_level
    #   （学生自报新水平）时才覆盖旧值，新轮不重置。
    #   为什么语义不同：意图回答「这一轮用户想干什么」，属于单轮；
    #   水平回答「这个学生是谁」，属于跨轮持续的画像，若每轮重置，
    #   已建立的水平画像会丢失，分层讲解也随之失效。
    # - 首次提问无水平信息：level 保持 None（初始默认），读取侧按
    #   StudentLevel.UNKNOWN 处理（默认中等深度讲解，见 prompts.py）。
    #
    # 为什么放 state 而不是只放 task_context：state 是跨轮画像的权威
    # 来源，无论本轮是否分派任务都保留（直接回答轮同样记录学生水平）；
    # task_context.level 只是分派时的快照（与 task_context.intent 同构），
    # 供 Worker 读取。
    level: Annotated[str | None, _replace]

    # --- 任务上下文 ---
    # 跨轮持久的结构化任务信息（由 Supervisor 填充）
    task_context: Annotated[TaskContext | None, _replace]

    # 当前用户轮次的显式有序任务计划，是结果 sequence/目标映射的事实来源。
    task_plan: Annotated[TaskPlan | None, _replace]

    # 当前计划的终态步骤结果；串行执行时整表原子替换，避免重放追加重复项。
    task_results: Annotated[list[TaskStepResult], _replace]

    # --- 工具调用结果 ---
    # 追加式累积，保留完整调用历史供审计
    tool_results: Annotated[list[ToolResult], operator.add]

    # --- 会话元信息 ---
    session_id: Annotated[str | None, _replace]
    user_id: Annotated[str | None, _replace]

    events: Annotated[list[RunEvent], operator.add]
    run_error: Annotated[RunError | None, _replace]
    handoff_count: Annotated[int, _replace]
    agent_switch_count: Annotated[int, _replace]

    # --- 扩展预留 ---
    # 跨轮持久的自由格式附加数据，避免频繁修改 Schema
    extra: Annotated[dict[str, Any], _replace]


# ─────────────────────────────────────────────
# 工厂函数
# ─────────────────────────────────────────────


def create_initial_state(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
) -> AgentState:
    """创建空白初始状态，用于启动一次新的图执行.

    Args:
        session_id: 会话唯一标识（多会话隔离）
        user_id: 用户唯一标识

    Returns:
        所有字段已填充默认值的 AgentState 实例
    """
    return AgentState(
        messages=[],
        current_agent=None,
        next_agent=None,
        pending_handoff=None,
        intent=None,
        # S2-T2 学生水平画像：初始为 None（「尚未识别任何水平」），
        # 跨轮保留、不随新轮重置；读取侧按 StudentLevel.UNKNOWN 归一。
        level=None,
        task_context=None,
        task_plan=None,
        task_results=[],
        tool_results=[],
        session_id=session_id,
        user_id=user_id,
        events=[],
        run_error=None,
        handoff_count=0,
        agent_switch_count=0,
        extra={},
    )
