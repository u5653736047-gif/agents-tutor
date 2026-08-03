"""安全、精简的运行事件模型。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """运行时可观察事件。"""

    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    AGENT_SWITCHED = "agent_switched"
    # S2-T1 意图识别：Supervisor 完成本轮意图分类后发出（agent=supervisor，
    # intent=意图枚举值字符串）。事件是瞬时的运行时信号，权威值以 state["intent"]
    # 为准（checkpoint 持久化）；消费方（api/chat.py 的 EVENT_TYPE_MAP 白名单）
    # 对未映射的新事件类型安全跳过，因此新增类型不会破坏既有事件协议。
    INTENT_DETECTED = "intent_detected"
    TASK_RESULT_ARCHIVED = "task_result_archived"
    TASK_RESULTS_AGGREGATED = "task_results_aggregated"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class ErrorCode(StrEnum):
    """稳定的运行时错误分类。"""

    TOOL_UNKNOWN = "tool_unknown"
    TOOL_UNAUTHORIZED = "tool_unauthorized"
    TOOL_INVALID_ARGUMENTS = "tool_invalid_arguments"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_TIMEOUT = "tool_timeout"
    MODEL_CALL_FAILED = "model_call_failed"
    REACT_ITERATION_LIMIT = "react_iteration_limit"
    GRAPH_HANDOFF_LIMIT = "graph_handoff_limit"
    GRAPH_SWITCH_LIMIT = "graph_switch_limit"
    GRAPH_INVALID_TARGET = "graph_invalid_target"
    GRAPH_AGGREGATION_INVALID = "graph_aggregation_invalid"
    AGENT_OUTPUT_INVALID = "agent_output_invalid"


class RunEvent(BaseModel):
    """不携带内容、参数或密钥的运行事件。"""

    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    sequence: int = Field(ge=0)
    session_id: str | None
    agent: str | None = None
    tool_name: str | None = None
    success: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: ErrorCode | None = None
    plan_step_sequence: int | None = Field(default=None, ge=1)
    degraded: bool | None = None
    # S2-T1：INTENT_DETECTED 事件携带的意图枚举值（如 "lesson_prep"）。
    # 放在事件本体而不是塞进 agent/tool_name，是为了让消费方按字段读取，
    # 与 TOOL_COMPLETED 携带 tool_name 的既有约定保持一致。
    intent: str | None = None


class RunError(BaseModel):
    """失败事件使用的最小诊断信息。"""

    model_config = ConfigDict(extra="forbid")

    error_code: ErrorCode
    message: str
    agent: str | None = None


__all__ = ["ErrorCode", "EventType", "RunError", "RunEvent"]
