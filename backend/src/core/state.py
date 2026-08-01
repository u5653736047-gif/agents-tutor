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
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

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


class TaskStatus(StrEnum):
    """任务生命周期状态."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ─────────────────────────────────────────────
# Pydantic 子模型（嵌套结构）
# ─────────────────────────────────────────────


class TaskContext(BaseModel):
    """当前任务的结构化上下文.

    由 Supervisor 在任务分解阶段填充，
    各子 Agent 读取自身相关的任务信息执行工作。
    """

    task_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    intent: str = Field(default="", description="用户意图分类标签")
    description: str = Field(default="", description="任务自然语言描述")
    subtasks: list[str] = Field(default_factory=list, description="分解后的子任务列表")
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展元数据（难度级别、学科标签、关联知识点等）",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SubtaskResult(BaseModel):
    """单个子任务的结构化执行结果.

    由子 Agent 节点在任务执行完毕后追加到状态，
    Supervisor 据此聚合判断并行子任务是否全部完成。
    """

    task_id: str = Field(description="所属任务（TaskContext）的唯一 ID")
    subtask: str = Field(default="", description="子任务文本描述")
    worker: AgentRole = Field(description="执行该子任务的 Agent 角色")
    output: str = Field(default="", description="子任务执行结果文本")
    success: bool = Field(default=True)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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
    duration_ms: float = Field(default=0.0, description="执行耗时（毫秒）")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ─────────────────────────────────────────────
# Reducer 函数
# ─────────────────────────────────────────────


def _replace(existing: Any, new: Any) -> Any:
    """直接覆盖式 reducer：新值非 None 时替换旧值."""
    return new if new is not None else existing


# ─────────────────────────────────────────────
# 全局状态定义（LangGraph StateGraph 入口）
# ─────────────────────────────────────────────


class AgentState(TypedDict, total=False):
    """多智能体系统全局状态.

    基于 TypedDict 定义，与 LangGraph StateGraph 状态通道机制原生兼容。
    各字段通过 Annotated 指定 reducer 控制并发更新语义：
    - messages: 追加合并（由 langgraph add_messages 处理去重与按 ID 更新）
    - tool_results: 追加合并（operator.add 拼接列表）
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

    # --- 任务上下文 ---
    # 结构化的当前任务信息（由 Supervisor 填充）
    task_context: Annotated[TaskContext | None, _replace]

    # --- 子任务结果聚合 ---
    # 追加式累积（整个会话生命周期只增不减），Supervisor 按当前 task_id
    # 过滤计数后与计划总数对比，判断本轮并行子任务是否全部完成
    subtask_results: Annotated[list[SubtaskResult], operator.add]

    # --- 工具调用结果 ---
    # 追加式累积，保留完整调用历史供审计
    tool_results: Annotated[list[ToolResult], operator.add]

    # --- 会话元信息 ---
    session_id: Annotated[str | None, _replace]
    user_id: Annotated[str | None, _replace]

    # --- 扩展预留 ---
    # 自由格式附加数据，避免频繁修改 Schema
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
        task_context=None,
        subtask_results=[],
        tool_results=[],
        session_id=session_id,
        user_id=user_id,
        extra={},
    )
