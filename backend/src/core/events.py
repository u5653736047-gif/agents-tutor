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


class RunError(BaseModel):
    """失败事件使用的最小诊断信息。"""

    model_config = ConfigDict(extra="forbid")

    error_code: ErrorCode
    message: str
    agent: str | None = None


__all__ = ["ErrorCode", "EventType", "RunError", "RunEvent"]
