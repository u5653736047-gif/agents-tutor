"""Stable external API contracts for the bridge layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import models_json_schema


class ContractModel(BaseModel):
    """Base model that rejects undeclared external fields."""

    model_config = ConfigDict(extra="forbid")


class AgentRole(str, Enum):
    """Public collaborative-agent roles."""

    SUPERVISOR = "supervisor"
    TEACHING_ASSISTANT = "teaching_assistant"
    LEARNING_ASSISTANT = "learning_assistant"
    EVALUATOR = "evaluator"


class WorkerAgentRole(str, Enum):
    """Roles that can receive a handoff or task-plan step."""

    TEACHING_ASSISTANT = "teaching_assistant"
    LEARNING_ASSISTANT = "learning_assistant"
    EVALUATOR = "evaluator"


class MessageRole(str, Enum):
    """Safe message roles exposed to API clients."""

    USER = "user"
    ASSISTANT = "assistant"


class StreamEventType(str, Enum):
    """Public event protocol reserved for future streaming support."""

    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MESSAGE_END = "message_end"
    AGENT_SWITCH = "agent_switch"
    ERROR = "error"
    DONE = "done"


class ErrorCode(str, Enum):
    """Stable API error codes aligned with the current Core classification."""

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


class ApiErrorCode(str, Enum):
    """Stable HTTP API errors not emitted by a graph run."""

    INVALID_REQUEST = "invalid_request"
    INTERNAL_ERROR = "internal_error"
    HANDOFF_NOT_PENDING = "handoff_not_pending"
    SESSION_ALREADY_EXISTS = "session_already_exists"
    SESSION_BUSY = "session_busy"
    SESSION_NOT_FOUND = "session_not_found"


class TaskPlanStatus(str, Enum):
    """Public task-plan status values."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Session(ContractModel):
    """A session visible to its owner."""

    session_id: str
    user_id: str | None
    created_at: datetime
    archived: bool


class CreateSessionRequest(ContractModel):
    """Optional client-selected ID for a new session."""

    session_id: str | None = None


class ErrorDetail(ContractModel):
    """A sanitized stable API error."""

    error_code: ApiErrorCode
    message: str


class ErrorResponse(ContractModel):
    """FastAPI's standard error envelope."""

    detail: ErrorDetail


class ChatRequest(ContractModel):
    """One synchronous user message for a session."""

    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)

    @field_validator("session_id", "message")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject whitespace-only values before they reach Core or persistence."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class Message(ContractModel):
    """A safe user or assistant message."""

    role: MessageRole
    content: str
    agent: AgentRole | None = None
    created_at: datetime | None = None


class RunEvent(ContractModel):
    """A safe incremental event emitted during one run."""

    event_type: StreamEventType
    sequence: int = Field(ge=0)
    session_id: str | None = None
    agent: AgentRole | None = None
    tool_name: str | None = None
    success: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: ErrorCode | None = None
    plan_step_sequence: int | None = Field(default=None, ge=1)
    degraded: bool | None = None


class StreamEvent(ContractModel):
    """SSE 流式事件(D1-T1):基于 RunEvent 扩展内容字段。

    事件安全红线(与 core/events.RunEvent「不携带内容、参数或密钥」
    的注释、api/chat.py 的 EVENT_TYPE_MAP 白名单同口径):
    - tool_call / tool_result 事件由 _public_event 映射而来,只含工具名、
      成功与否、耗时等摘要,绝不含工具参数与结果正文;
    - thinking 事件的 content 只放固定占位文本(如 Agent 名),绝不伪造
      模型中间输出;
    - message_end 事件的 content 是最终消息全文(与 POST /chat 的
      ChatResponse.message.content 同源)。
    error_code 复用 RunError 的联合类型:流式 error 事件需要携带
    ApiErrorCode(SESSION_BUSY / INTERNAL_ERROR),仅 ErrorCode 装不下。
    """

    event_type: StreamEventType
    sequence: int = Field(ge=0)
    session_id: str
    agent: AgentRole | None = None
    tool_name: str | None = None
    success: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: ErrorCode | ApiErrorCode | None = None
    plan_step_sequence: int | None = Field(default=None, ge=1)
    content: str | None = None  # thinking 占位 / message_end 全文
    message: Message | None = None  # message_end 的完整消息(可选)
    citations: list[Citation] | None = None
    current_agent: AgentRole | None = None


class RunError(ContractModel):
    """A stable, sanitized error response for a completed run."""

    error_code: ErrorCode | ApiErrorCode
    message: str
    agent: AgentRole | None = None


class HandoffRequest(ContractModel):
    """The information a user reviews before a worker handoff."""

    target_agent: WorkerAgentRole
    task_content: str
    plan_step_sequence: int | None = Field(default=None, ge=1)


class PendingHandoff(ContractModel):
    """A handoff paused for a future confirmation or rejection."""

    interrupt_id: str
    request: HandoffRequest


class PendingHandoffResponse(ContractModel):
    """The current handoff approval state for one session."""

    session_id: str
    pending_handoff: PendingHandoff | None = None


class HandoffDecisionAction(str, Enum):
    """Approval actions supported by the skeleton API."""

    CONFIRM = "confirm"
    REJECT = "reject"


class HandoffDecisionRequest(ContractModel):
    """A confirmation or rejection for one pending handoff interrupt."""

    interrupt_id: str = Field(min_length=1)
    action: HandoffDecisionAction
    target_agent: WorkerAgentRole | None = Field(
        default=None,
        description="Reserved for a future modification workflow.",
    )
    task_content: str | None = Field(
        default=None,
        description="Reserved for a future modification workflow.",
    )

    @field_validator("interrupt_id")
    @classmethod
    def reject_blank_interrupt_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def reject_modification_fields(self) -> HandoffDecisionRequest:
        """Reserve modification fields without implementing that workflow yet."""
        if self.target_agent is not None or self.task_content is not None:
            raise ValueError("handoff modifications are not supported")
        return self

class Citation(ContractModel):
    """A safe reference placeholder for future retrieval-backed responses."""

    document_id: str
    source: str
    page: int | None = Field(default=None, ge=1)
    chunk_id: str


class TaskPlanStep(ContractModel):
    """One planned worker task."""

    sequence: int = Field(ge=1)
    description: str
    target_agent: WorkerAgentRole


class TaskPlan(ContractModel):
    """A task plan reserved for future chat responses."""

    steps: list[TaskPlanStep] = Field(min_length=2)
    current_step_index: int = Field(ge=0)
    status: TaskPlanStatus


class TaskResult(ContractModel):
    """A completed or failed task-plan step."""

    step_sequence: int = Field(ge=1)
    target_agent: WorkerAgentRole
    success: bool
    output: str | None = None
    error_code: ErrorCode | None = None


class ChatResponse(ContractModel):
    """The synchronous response contract shared by chat and approval routes."""

    session_id: str
    message: Message | None = None
    events: list[RunEvent] = Field(default_factory=list)
    run_error: RunError | None = None
    pending_handoff: PendingHandoff | None = None
    references: list[Citation] | None = None
    task_plan: TaskPlan | None = None
    task_results: list[TaskResult] | None = None
    current_agent: AgentRole | None = None


CONTRACT_MODELS: tuple[type[ContractModel], ...] = (
    Session,
    CreateSessionRequest,
    ErrorDetail,
    ErrorResponse,
    ChatRequest,
    Message,
    RunEvent,
    StreamEvent,
    RunError,
    HandoffRequest,
    PendingHandoff,
    PendingHandoffResponse,
    HandoffDecisionRequest,
    Citation,
    TaskPlanStep,
    TaskPlan,
    TaskResult,
    ChatResponse,
)


def contract_openapi_schemas() -> dict[str, Any]:
    """Return Pydantic schemas for external models not yet used by a route."""
    _, schema = models_json_schema(
        [(model, "validation") for model in CONTRACT_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    return dict(schema["$defs"])
