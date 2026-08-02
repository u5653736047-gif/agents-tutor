"""Stable external API contracts for the bridge layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
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


class RunError(ContractModel):
    """A stable, sanitized error response for a completed run."""

    error_code: ErrorCode
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
    """The synchronous chat response contract reserved for W0-T4."""

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
    Message,
    RunEvent,
    RunError,
    HandoffRequest,
    PendingHandoff,
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
