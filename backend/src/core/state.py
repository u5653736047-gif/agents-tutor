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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .events import ErrorCode, RunError, RunEvent
from .knowledge.models import Citation

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
    - DIAGNOSIS 学情诊断（六大功能 P3）：薄弱点分析/学情报告/学习
      预警 → evaluator（基于学习记录聚合写诊断报告）；
    - LEARNING_PATH 学习路径规划（六大功能 P4）：规划学习计划/推送
      资源 → learning_assistant（读学习记录 + 难度过滤检索）；
    - STUDY_COACHING 学习陪伴（六大功能 P5）：知识点巩固/错题归因/
      学习策略 → learning_assistant（导师/学伴语气，苏格拉底式引导）；
    - OTHER 其他：模型能确定但不在上述类别 → 直接回答；
    - UNCLEAR 意图不明：模型无法确定 → Supervisor 必须追问澄清，
      禁止 handoff 或 create_task_plan（graph_builder 有运行时兜底拦截）。
    """

    ANSWER_QUESTION = "answer_question"
    LESSON_PREP = "lesson_prep"
    EVALUATION = "evaluation"
    # 六大功能 P3-P5：三个新意图（子集断言安全加法，见
    # test_intent_recognition.py 的 <= 口径）。
    DIAGNOSIS = "diagnosis"
    LEARNING_PATH = "learning_path"
    STUDY_COACHING = "study_coaching"
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


class EvaluationVerdict(StrEnum):
    """评价结论枚举（S2-T3 基础评价规则）。

    语义（为什么是这三档，对应验收标准「通过/存疑/不通过」）：
    - PASS 通过：回答的事实准确、引用完整，可以直接采纳；
    - QUESTIONABLE 存疑：存在轻微瑕疵（个别表述不准、个别引用缺失），
      需要学生或教师复核，但不至于整体否定；
    - FAIL 不通过：存在事实错误或引用严重缺失，回答不可直接采纳。

    枚举值（字符串形式，见 AgentState.evaluation 注释）会写入
    state["evaluation"]（随 checkpoint 持久化）与
    EVALUATION_COMPLETED 运行事件；单维度（事实准确性 / 引用完整性）
    与总结论共用同一枚举，避免为「维度结论」再维护一套平行枚举。
    与 Intent / StudentLevel 一样可扩展：新增档位只需追加枚举值。
    """

    PASS = "pass"
    QUESTIONABLE = "questionable"
    FAIL = "fail"


class EvaluationDimension(StrEnum):
    """评价维度枚举（S2-T3 骨架期两个最小可用维度）。

    - FACT_ACCURACY 事实准确性：回答内容与检索证据/事实是否一致；
    - CITATION_COMPLETENESS 引用完整性：回答是否引用了本轮检索证据
      （无证据可引时该维度应判存疑/不通过，见 prompts.py evaluator
      约定与 graph_builder.py submit_evaluation 注释）。

    与 S2-T4（引用插入）的分工：本任务只做「评价规则」本身，能区分
    「有引用 / 无引用」即可；Citation 字段的完整插入链路属 S2-T4。
    """

    FACT_ACCURACY = "fact_accuracy"
    CITATION_COMPLETENESS = "citation_completeness"


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


# ─────────────────────────────────────────────
# 助手消息的结构化引用元数据（S2-T4 最终回答引用插入）
# ─────────────────────────────────────────────

# 当 Agent 使用检索工具（search_knowledge 等）的证据作答时，最终回答
# 消息会在 additional_kwargs 中写入「本轮检索命中的结构化引用列表」，
# 供前端渲染引用与评价 Agent 校验（API 层 ChatResponse.references
# 字段的消费属 D3-T5，读取入口就是本模块的 message_references——
# API 层只需从 get_history() 的最后一条助手消息读出即可，零改动接入）。
#
# 为什么用 additional_kwargs（消息元数据）而不是独立的 state 通道：
# 1) 引用是「这条回答用了哪些证据」的随消息属性——挂在消息上与内容
#    同生命周期、同序列化路径，get_history() 读出的消息天然携带，
#    前端按消息渲染引用与角色徽章（AGENT_ROLE_METADATA_KEY）同机制；
#    若放 state 独立通道，消息与引用会分离存储，恢复历史时还要按轮次
#    重新配对，API 层消费改动更大；
# 2) additional_kwargs 是 LangChain 消息的标准附加字段，LangGraph 的
#    SQLite checkpointer（JsonPlusSerializer，msgpack 基）序列化消息时
#    原样保留——与角色元数据是同一已验证路径（test_agent_role_metadata
#    覆盖序列化往返），进程重建后 get_history() 仍能恢复引用列表；
# 3) 值存 dict 列表而非 Citation 对象列表：additional_kwargs 里的值
#    必须是 msgpack 原生类型才能保证序列化往返无自定义类型注册依赖
#    （与 intent/level 存枚举值字符串同理），因此写入端把 Citation
#    转 model_dump(mode="json") 的 dict，读取端用 Citation.model_validate
#    宽容还原。
#
# 写入端严格（只接受 Citation 模型，输出固定 JSON 结构），读取端宽容
# （见 message_references）：宁可返回 None 也不让异常数据击穿前端。
REFERENCES_METADATA_KEY = "references"


def with_references(
    message: AIMessage,
    citations: Sequence[Citation],
) -> AIMessage:
    """返回携带结构化引用列表的 AIMessage 副本（不修改原对象）。

    与 with_agent_role 同一副本语义（理由相同）：模型返回的 AIMessage
    可能被调用方复用（如再次作为模型输入），就地修改会污染模型看到的
    历史；model_copy 只替换 additional_kwargs，content、tool_calls、
    response_metadata 等字段原样保留，既有 additional_kwargs（如 provider
    元数据、AGENT_ROLE_METADATA_KEY）也保留，只新增
    REFERENCES_METADATA_KEY 一个键。
    引用以 dict 列表形式写入（见上方模块注释第 3 点），保证 checkpoint
    序列化往返无需类型注册。

    空序列防御（接口契约完整性）：citations 为空时原样返回消息、不注入
    空的 references 键——「无引用就不携带引用」是 S2-T4 的零命中语义，
    空键会与「有引用但列表为空」的脏状态混淆。当前唯一调用方
    _attach_references 在收集为空时根本不会调用本函数（保证传入非空），
    此防御面向未来新增调用方，避免意外写入空键。
    """
    if not citations:
        return message
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs[REFERENCES_METADATA_KEY] = [
        citation.model_dump(mode="json") for citation in citations
    ]
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def message_references(message: BaseMessage) -> list[Citation] | None:
    """从消息元数据读出结构化引用列表；无法确定时返回 None。

    读取端宽容（与 message_agent_role 同一哲学）：
    - 键缺失或值不是列表 → None，表示「该消息没有引用」而非数据错误；
    - 列表内的非法项（历史脏数据、未来字段变更）逐项跳过，合法项照常
      返回；列表本身合法但无任何可解析项时返回空列表——保证
      get_history() 的消费者（前端引用渲染、API 层 references 组装）
      不会因异常崩溃。
    """
    raw = message.additional_kwargs.get(REFERENCES_METADATA_KEY)
    if not isinstance(raw, list):
        return None
    citations: list[Citation] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            citations.append(Citation.model_validate(item))
        except ValidationError:
            continue
    return citations


# ─────────────────────────────────────────────
# 助手消息的生成文件元数据（T5-3 下载回执）
# ─────────────────────────────────────────────
#
# officecli_edit 写工具成功后，结果会携带 generated_files 清单（见
# core/tools/office_tools.py）；图在 _wrap 闸口把清单挂到本轮终端回答
# 消息的 additional_kwargs——与 references 同一机制、同一序列化路径，
# checkpoint 往返保留。API 层读取后把工作区文件注册为受控下载附件
# （api/files.py），前端按消息渲染下载入口。
GENERATED_FILES_METADATA_KEY = "generated_files"


class GeneratedFile(BaseModel):
    """一次写操作产出的工作区文件回执元数据（msgpack 原生可序列化）。

    - path：授权绝对路径（core 侧唯一事实来源，绝不直接暴露给前端）；
    - name：展示文件名（纯文件名，不含目录）；
    - size / mtime_ns：写入完成时刻的大小与修改时间，API 层据此派生
      版本化下载 ID（同一文件多轮修改各是各的回执）。
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)


def with_generated_files(
    message: AIMessage,
    files: Sequence[GeneratedFile],
) -> AIMessage:
    """返回携带生成文件清单的 AIMessage 副本（不修改原对象）。

    与 with_references 同一副本语义与空序列防御：files 为空时原样返回、
    不注入空键（「无生成文件就不携带」的语义，与零命中不挂引用一致）。
    """
    if not files:
        return message
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs[GENERATED_FILES_METADATA_KEY] = [
        file.model_dump(mode="json") for file in files
    ]
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def message_generated_files(message: BaseMessage) -> list[GeneratedFile] | None:
    """从消息元数据读出生成文件清单；无法确定时返回 None。

    读取端宽容（与 message_references 同一哲学）：键缺失或值不是列表
    → None；列表内的非法项逐项跳过，合法项照常返回；保证
    get_history() 的消费者（API 附件组装、前端下载入口）不会因异常
    数据崩溃。
    """
    raw = message.additional_kwargs.get(GENERATED_FILES_METADATA_KEY)
    if not isinstance(raw, list):
        return None
    files: list[GeneratedFile] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            files.append(GeneratedFile.model_validate(item))
        except ValidationError:
            continue
    return files


# ─────────────────────────────────────────────
# 助手消息的批改结果元数据（六大功能 P2-12 历史回放）
# ─────────────────────────────────────────────
#
# 批改结果（GradingResult）除写入 state["grading"] 通道（当轮直出
# ChatResponse.grading）外，还挂到本轮终端回答消息的 additional_kwargs
# ——与 references 同一机制、同一序列化路径。为什么必须挂消息元数据
# （pi 审查 🟡4）：state 通道的 grading 每轮重置，且 SessionProcess
# 只是最近一轮快照——批改发生在更早轮次时，刷新/切会话后批改卡会
# 消失；挂在消息上则任意历史轮的批改卡都能经 history 端点恢复。
GRADING_METADATA_KEY = "grading"


def with_grading(message: AIMessage, grading: GradingResult) -> AIMessage:
    """返回携带批改结果的 AIMessage 副本（不修改原对象）。

    与 with_references 同一副本语义：模型返回的 AIMessage 可能被调用
    方复用，就地修改会污染模型看到的历史；model_copy 只替换
    additional_kwargs，既有键（角色/references/generated_files）保留。
    值存 model_dump(mode="json") 的 dict：msgpack 原生类型，checkpoint
    序列化往返无需类型注册（与 references 同理）。
    """
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs[GRADING_METADATA_KEY] = grading.model_dump(mode="json")
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def message_grading(message: BaseMessage) -> GradingResult | None:
    """从消息元数据读出批改结果；无法确定时返回 None。

    读取端宽容（与 message_references 同一哲学）：键缺失/值不是 dict
    → None；脏数据（历史数据、未来字段变更）校验失败 → None，保证
    get_history() 的消费者（前端批改卡渲染）不会因异常崩溃。
    """
    raw = message.additional_kwargs.get(GRADING_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return GradingResult.model_validate(raw)
    except ValidationError:
        return None


# 可被分派执行任务的 Worker 角色子集（Supervisor 只调度、不执行）
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


class ToolApprovalAction(StrEnum):
    """Human decision for one exact approval-gated tool call."""

    CONFIRM = "confirm"
    REJECT = "reject"


# ─────────────────────────────────────────────
# Pydantic 子模型（嵌套结构）
# ─────────────────────────────────────────────


class TaskContext(BaseModel):
    """当前任务的结构化上下文.

    由 Supervisor 在任务分解阶段填充，
    各子 Agent 读取自身相关的任务信息执行工作。
    """

    # 12 位随机短 ID：任务唯一标识（短到方便展示，碰撞概率足够低）
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
    # S5-A2 步骤失败策略（core 内部字段，不扩 API 契约）：
    # - abort（默认）：步骤失败即计划 FAILED，后续计划内 ask_* 硬熔断；
    # - continue：记失败结果后推进游标继续后续步骤；
    # - retry：预算内允许同目标重试一次（不推进游标、计一次
    #   retries_used），重试再失败按 abort 收口。
    # 默认 abort 是有意的保守选择：未显式表达可跳过的步骤失败时，
    # 宁可熔断也不带着缺失结果聚合作答。提示词侧引导模型对资料收集
    # 类步骤显式设 continue（见 TOOL_ORCHESTRATION_SUPERVISOR_PROMPT）。
    on_failure: Literal["abort", "continue", "retry"] = "abort"

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
    # S5-A2 每计划重试预算的已用计数（有界防循环；预算常量在
    # graph_builder._TOOL_PLAN_RETRY_BUDGET）。core 内部字段不扩 API 契约。
    retries_used: int = Field(default=0, ge=0)

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


class ToolApprovalRequest(BaseModel):
    """An exact validated tool call waiting at a resumable graph gate."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    agent_role: AgentRole
    arguments: dict[str, Any]

    @field_validator("tool_call_id", "tool_name")
    @classmethod
    def identifiers_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier must not be blank")
        return value


class ToolApprovalDecision(BaseModel):
    """A stale-safe decision for one pending approval-gated tool call."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1)
    action: ToolApprovalAction

    @field_validator("interrupt_id")
    @classmethod
    def interrupt_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("interrupt_id must not be blank")
        return value


class PendingToolApproval(BaseModel):
    """Public core view of a tool gate and its exact proposed invocation."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1)
    request: ToolApprovalRequest


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


class ReferenceVerification(BaseModel):
    """引用真实性校验结论（S2-T5 核心校验层自动产出，非模型填写）。

    这是「引用真实性校验层」的产物：核心层在引用写入消息元数据之前，
    把「消息中已有的引用」与「本轮检索工具结果的真实命中」逐条比对，
    剔除伪造/越界条目、做文档级合并规范化后，把结论记录在这里
    （校验依据、判定逻辑与处置取舍的完整说明见 graph_builder.py 的
    _attach_references / _verify_references / _merge_citations_by_document
    注释）。

    字段语义：
    - total：最终挂载到消息上的引用条数（文档级合并之后）；
    - verified：经校验确认为真实命中的 **chunk 级** 引用条数——最终
      挂载列表由本轮真实命中（chunk 级全集）合并而来，chunk 级口径下
      每条都是真实命中，故 verified = chunk 级命中条数；文档级合并后
      展示为 total 条（total <= verified），merged = verified - total
      自洽（审计者可同时看到「校验了多少个 chunk、合并展示为几条」）。
      注意与「降级标记」模式的关系：若未来改为保留未验证条目，
      verified < chunk 级命中条数 即有语义；
    - removed：检测到并剔除的伪造/越界引用条数（来自消息中已有的
      引用——注入/脏数据的唯一来源，见 _attach_references 注释）；
    - merged：文档级合并减少的条数（chunk 级条数 - 文档级条数）；
    - removed_chunk_ids：被剔除引用的 chunk_id 列表（脱敏原则：只记
      结构化标识，不记任何正文/内容字段）；
    - merged_document_ids：被合并（非文档首条）chunk 所属 document_id
      列表——每个被合并文档只记录一次（M-2：第二次出现时记录、其后
      不再重复，保序）。

    为什么校验结论要独立成模型并写入 state（而不是只挂在消息上）：
    1) 校验结论是「本轮运行的事实记录」，随 checkpoint 持久化后
       get_state()/恢复会话仍可审计——与 evaluation 同机制；
    2) 它会被核心层并入 EvaluationResult.reference_verification
       （见该字段注释），评价 Agent 的结论与引用校验结论同处可读，
       构成「引用真实性」的审计闭环；
    3) 只记计数与结构化标识（chunk_id/document_id），不复制引用正文，
       与仓库「事件/审计字段不记敏感正文」的惯例一致。
    """

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0, description="最终挂载引用条数（文档级合并后）")
    verified: int = Field(
        ge=0,
        description="经校验为真实命中的 chunk 级条数（合并前口径，>= total）",
    )
    removed: int = Field(ge=0, description="检测到并剔除的伪造/越界引用条数")
    merged: int = Field(ge=0, description="文档级合并减少的条数")
    removed_chunk_ids: list[str] = Field(
        default_factory=list,
        description="被剔除引用的 chunk_id（脱敏：结构化标识，非正文）",
    )
    merged_document_ids: list[str] = Field(
        default_factory=list,
        description="被合并（非文档首条）chunk 所属 document_id（去重保序）",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvaluationResult(BaseModel):
    """评价 Agent 对一轮最终回答的结构化评价结论（S2-T3）。

    骨架期最小可用字段（对应验收标准）：
    - verdict 总结论（pass / questionable / fail，见 EvaluationVerdict 注释）；
    - fact_accuracy / citation_completeness 两个维度的独立结论
      （维度枚举见 EvaluationDimension 注释）；
    - reason 理由字符串（由 submit_evaluation 工具截断，见 graph_builder.py）；
    - evidence_tool_names 本轮检索证据的工具名列表——由核心层（_wrap）在
      解析 submit_evaluation 结果时自动组装（不是模型填写的），记录
      「这次评价基于哪些检索证据」，构成「不凭空评价」的可审计闭环：
      审计者看到证据工具列表 + 工具结果（state["tool_results"]）即可核对
      评价是否真的有依据。

    为什么 evaluation 结论放在 state 而不是只发事件（与 intent/level 同构）：
    1) checkpoint 持久化——事件只存在于当次运行的 events 列表中，跨轮不可查；
       state["evaluation"] 随 checkpoint 保存，get_state()/恢复会话后仍能
       读到上一轮的评价结论，是「后续审计读取」的事实来源；
    2) 脱敏分工——state 存完整结论（含 reason），事件只记录 verdict 摘要
       （见 events.py EVALUATION_COMPLETED 注释），二者对照即可在事件流
       上做轻量回放、在状态里做完整审计。
    """

    model_config = ConfigDict(extra="forbid")

    verdict: EvaluationVerdict = Field(description="总结论：通过/存疑/不通过")
    fact_accuracy: EvaluationVerdict = Field(description="事实准确性维度结论")
    citation_completeness: EvaluationVerdict = Field(
        description="引用完整性维度结论"
    )
    reason: str = Field(default="", description="评价理由（工具层已截断，有界）")
    # 核心层组装：本轮成功执行的检索/业务工具名（不含 submit_evaluation 本身），
    # 只记工具名不记输出正文——正文仍在 state["tool_results"] 中按工具结果审计。
    evidence_tool_names: list[str] = Field(
        default_factory=list,
        description="本轮评价依据的检索证据工具名列表（核心层组装，脱敏）",
    )
    # S2-T5 引用真实性校验结论（核心层组装，模型不可填写——submit_evaluation
    # 的 schema 没有该字段）。为什么并入评价结果：验收标准要求校验结论
    # 「在评价结果中体现」，读取方（API/审计）拿到 evaluation 即可同时看到
    # 引用校验结论（剔除/合并计数与明细），与 evidence_tool_names 的
    # 「证据由核心层确定」同一哲学。默认 None 向后兼容：旧 checkpoint 或
    # 未走校验层的评价没有该字段，读取端宽容（见测试
    # test_legacy_evaluation_without_verification_field_validates）。
    reference_verification: ReferenceVerification | None = Field(
        default=None,
        description="本轮引用真实性校验结论（核心层组装，默认 None 向后兼容）",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("reason")
    @classmethod
    def reason_must_be_bounded(cls, reason: str) -> str:
        # 写入端宽容：reason 允许为空（模型可能只给结论不给理由），
        # 但超长理由会在工具层截断（submit_evaluation 的 reason[:200]），
        # 这里兜底再截一次，保证 checkpoint 中的审计字段永远有界。
        return reason[:200]


# ─────────────────────────────────────────────
# 批改结果模型（六大功能 P2-8：作业与试题批改）
# ─────────────────────────────────────────────


class GradingItem(BaseModel):
    """一道题的批改结论（P2-8；pi 审查 🔴3 补知识点维度）。

    knowledge_point/error_tag 是学情诊断（功能 3）的主要数据源：
    _wrap 解析成功后逐题确定性落库 learning_records（P2-10），缺失
    知识点的题记为「未分类」参与总量统计。feedback 承载改进建议
    （赛题要求「提供评分依据与改进建议」），截断有界保证 checkpoint
    审计字段不膨胀（与 EvaluationResult.reason 同一哲学）。
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    # feedback/overall_comment 不设 Field 长度上限（审查 S3：与
    # EvaluationResult.reason 同一单口径——超长由 validator 截断而非
    # schema 拒绝，声明与实施单口径，避免误导容量推导）。
    feedback: str = ""
    knowledge_point: str | None = Field(default=None, max_length=120)
    error_tag: str | None = Field(default=None, max_length=60)

    @field_validator("feedback")
    @classmethod
    def feedback_must_be_bounded(cls, feedback: str) -> str:
        # 工具层已截断，这里兜底再截一次（同 reason_must_be_bounded）。
        return feedback[:300]

    @model_validator(mode="after")
    def score_must_not_exceed_max(self) -> GradingItem:
        if self.score > self.max_score:
            raise ValueError("score must not exceed max_score")
        return self


class GradingResult(BaseModel):
    """一次批改的结构化结论（P2-8；与 EvaluationResult 语义区分——
    pi 审查拒绝方案 4：批改是逐题得分/反馈，不复用「评价系统回答」
    的三枚举维度，避免污染审计语义）。

    total_score / max_total_score 由核心侧（_wrap）从 items 确定性
    汇总——模型只提交逐题结论与总评，总分不信任模型自报（与
    evidence_tool_names「证据由核心侧确定」同一哲学）。
    """

    model_config = ConfigDict(extra="forbid")

    items: list[GradingItem] = Field(min_length=1, max_length=50)
    overall_comment: str = ""
    total_score: float = Field(ge=0)
    max_total_score: float = Field(gt=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("overall_comment")
    @classmethod
    def overall_comment_must_be_bounded(cls, comment: str) -> str:
        return comment[:500]


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

    # 已通过工具 schema 校验、等待用户确认的精确调用。工具调用消息先
    # 持久化，再进入独立 gate，恢复时不会重放产生调用的模型请求。
    pending_tool_approval: Annotated[ToolApprovalRequest | None, _replace]

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

    # --- 评价结论（S2-T3） ---
    # 评价 Agent 对一轮最终回答的结构化评价结论（EvaluationResult 模型）；
    # last-write-wins，由 submit_evaluation 工具结果经 _wrap 校验后写入，
    # run() 在新用户轮次重置为 None（与 intent 同构：评价是「这一轮的
    # 结论」，每轮重新评价；若跨轮保留，上一轮的评价徽章会误导后续轮次
    # 的展示与审计）。
    #
    # 为什么放 state 而不是只发事件：
    # 1) checkpoint 持久化——事件只存在于当次运行的 events 列表中，跨轮
    #    不可查；state 字段随 checkpoint 保存，get_state()/恢复会话后
    #    仍能读到上一轮的评价结论，是「后续审计读取」的事实来源；
    # 2) 脱敏分工——state 存完整结论（含 reason 与证据工具名列表），
    #    EVALUATION_COMPLETED 事件只记录 verdict 摘要（不记录 reason
    #    等可能含敏感正文的字段，见 events.py 注释），审计者按需选择
    #    轻量事件流或完整状态；
    # 3) 权威值——ToolResult 只记录「模型声称的评价」，state["evaluation"]
    #    是 _wrap 校验后的权威结论（枚举由工具 schema 严格约束），两者
    #    对照可以发现模型谎报或乱填。
    # 评价输入（最终回答 + 本轮检索证据）如何组装：最终回答天然在模型
    # 可见的消息历史中；检索证据以 ToolResult 形式进入模型上下文（ReAct
    # 循环的 ToolMessage 观察）供模型判断，_wrap 解析时再把本轮证据工具
    # 名组装进 EvaluationResult.evidence_tool_names（不记正文，正文仍按
    # tool_results 审计）——详见 graph_builder.py 的 submit_evaluation
    # 与 _wrap 注释。
    # 类型取舍：与 task_context / tool_results 通道一致，本通道直接存
    # Pydantic 模型（EvaluationResult 含 StrEnum 枚举字段，序列化为
    # 字符串）而不是像 intent/level 那样存裸字符串——嵌套模型由 Pydantic
    # 统一处理序列化，checkpoint 往返后的反序列化形式（dict 或模型实例，
    # 视序列化器而定）由读取方宽容处理（测试已兼容两种形式）。
    evaluation: Annotated[EvaluationResult | None, _replace]

    # --- 批改结论（六大功能 P2-8） ---
    # evaluator 对一次作业/试题批改的结构化结论（GradingResult 模型）；
    # last-write-wins，由 submit_grading 工具结果经 _wrap 校验后写入，
    # run() 在新用户轮次重置为 None（与 evaluation 同构：批改结论是
    # 「这一轮的成果」，历史轮次的批改卡经消息元数据恢复——见
    # GRADING_METADATA_KEY 注释，不靠通道跨轮保留）。
    grading: Annotated[GradingResult | None, _replace]

    # --- 引用真实性校验结论（S2-T5） ---
    # 引用校验层对本轮引用的自动校验结论（ReferenceVerification 模型）；
    # last-write-wins，由 _wrap 在引用写入消息元数据时同步产出，
    # run() 在新用户轮次重置为 None（与 evaluation 同构：校验结论是
    # 「本轮引用的事实记录」，每轮重新校验；跨轮保留会让审计者误以为
    # 旧轮结论属于新轮）。
    #
    # 为什么放 state 而不是只发事件：
    # 1) checkpoint 持久化——校验结论随 checkpoint 保存，get_state()/
    #    恢复会话后仍能读到上一轮的引用校验结论，是「引用真实性审计」
    #    的事实来源（与 evaluation 同一机制）；
    # 2) 评价联动——核心层在 evaluator 轮组装 EvaluationResult 时并入
    #    引用校验结论：优先用 evaluator 轮自身结论，否则回退并入本通道
    #    中「本用户轮先前轮次」的结论（如计划流程中 worker 轮的剔除
    #    明细，见 graph_builder.py _wrap 注释），评价结果与引用校验
    #    结论同处可读；
    # 3) 脱敏——本通道只记计数与结构化标识（chunk_id/document_id），
    #    不复制引用正文，符合「审计字段不记敏感正文」的仓库惯例。
    # 写入语义（见 graph_builder.py _attach_references 注释）：本轮
    # 挂载了引用或剔除了伪造才写入（非 None）；无检索无引用的全零
    # 场景不写（保持 None），与 evaluation 的「无评价→None」一致。
    # 类型取舍：与 evaluation 一致直接存 Pydantic 模型（嵌套模型由
    # Pydantic 统一处理序列化，读取方宽容处理 dict/模型实例两种形式）。
    reference_verification: Annotated[ReferenceVerification | None, _replace]

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

    # --- 生成文件回执（T5-3） ---
    # 审批门（_approve_tool）执行 officecli_edit 成功后写入本通道；
    # _wrap 把它挂到本轮终端回答消息的 additional_kwargs 后清空。
    # 不用 operator.add 而用整体替换：回执是「最近一次批准的写操作」
    # 语义，跨用户轮次在 _new_run_state 重置为空，不能跨轮累积
    # （否则新一轮的回答会重复携带旧轮次的下载入口）。
    generated_files: Annotated[list[GeneratedFile], _replace]

    # --- 会话元信息 ---
    session_id: Annotated[str | None, _replace]
    user_id: Annotated[str | None, _replace]
    # 当前用户消息对应的运行标识。每次 run/stream 生成新值，事件据此分轮；
    # 旧 checkpoint 没有该字段时按 None 兼容读取。
    run_id: Annotated[str | None, _replace]
    workspace_root: Annotated[str | None, _replace]
    additional_workspace_roots: Annotated[list[str], _replace]

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
    run_id: str | None = None,
    workspace_root: str | None = None,
    additional_workspace_roots: Sequence[str] = (),
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
        pending_tool_approval=None,
        intent=None,
        # S2-T2 学生水平画像：初始为 None（「尚未识别任何水平」），
        # 跨轮保留、不随新轮重置；读取侧按 StudentLevel.UNKNOWN 归一。
        level=None,
        # S2-T3 评价结论：初始为 None（「本轮尚无评价」），
        # 与 intent 同构、每轮重置（评价是单轮结论，见 AgentState.evaluation）。
        evaluation=None,
        # P2-8 批改结论：初始为 None（「本轮尚无批改」），
        # 与 evaluation 同构、每轮重置（历史批改经消息元数据恢复）。
        grading=None,
        # S2-T5 引用真实性校验结论：初始为 None（「本轮尚无校验内容」），
        # 与 evaluation 同构、每轮重置（校验结论是单轮事实记录）。
        reference_verification=None,
        task_context=None,
        task_plan=None,
        task_results=[],
        tool_results=[],
        generated_files=[],
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        workspace_root=workspace_root,
        additional_workspace_roots=list(additional_workspace_roots),
        events=[],
        run_error=None,
        handoff_count=0,
        agent_switch_count=0,
        extra={},
    )
