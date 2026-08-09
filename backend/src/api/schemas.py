"""Stable external API contracts for the bridge layer."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

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
    """Public SSE protocol for token deltas and safe execution events."""

    THINKING = "thinking"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_OUTPUT = "tool_output"
    APPROVAL_REQUIRED = "approval_required"
    MESSAGE_DELTA = "message_delta"
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
    TOOL_NO_PROGRESS = "tool_no_progress"
    TOOL_BUDGET_EXCEEDED = "tool_budget_exceeded"
    TOOL_APPROVAL_REJECTED = "tool_approval_rejected"
    TOOL_APPROVAL_QUEUE_LIMIT = "tool_approval_queue_limit"
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
    TOOL_APPROVAL_NOT_PENDING = "tool_approval_not_pending"
    SESSION_ALREADY_EXISTS = "session_already_exists"
    SESSION_BUSY = "session_busy"
    SESSION_NOT_FOUND = "session_not_found"
    KNOWLEDGE_UNAVAILABLE = "knowledge_unavailable"


class TaskPlanStatus(str, Enum):
    """Public task-plan status values."""

    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WorkspaceAccess(str, Enum):
    """Access level granted to Agent filesystem tools for a session."""

    READ_ONLY = "read_only"


class Session(ContractModel):
    """A session visible to its owner."""

    session_id: str
    user_id: str | None
    created_at: datetime
    updated_at: datetime
    archived: bool
    # 侧栏标题：首条用户消息提炼（只写一次）；存量老会话为 None，
    # 前端按 session_id 回退展示。
    title: str | None = None
    workspace_root: str
    additional_workspace_roots: list[str] = Field(default_factory=list)
    workspace_access: WorkspaceAccess = WorkspaceAccess.READ_ONLY


class CreateSessionRequest(ContractModel):
    """Optional client-selected ID for a new session."""

    session_id: str | None = None
    workspace_root: str | None = None

    @field_validator("workspace_root")
    @classmethod
    def reject_blank_workspace_root(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("workspace_root must not be blank")
        return value


class AddWorkspaceRootRequest(ContractModel):
    """One user-authorized additional workspace directory."""

    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def reject_blank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be blank")
        return value


class WorkspacePath(ContractModel):
    """A canonical existing directory accepted by the server policy."""

    path: str
    name: str


class WorkspaceDirectory(ContractModel):
    """One directory entry in the server-side workspace picker."""

    name: str
    path: str


class WorkspaceDirectoryListing(ContractModel):
    """Directory picker state rooted in the server filesystem."""

    path: str
    parent: str | None = None
    directories: list[WorkspaceDirectory] = Field(default_factory=list)


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
    # D7-T1:附件引用(契约扩展预留)——chat 路由当前忽略该字段(缺失
    # 或携带均不影响现有行为),由 D7-T3 或后续 core 能力决定如何进入
    # 模型上下文;留空列表与 None 等价。
    attachments: list[Attachment] | None = None

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
    # D7-T3:附件引用(可选;历史消息/非附件消息为 None)。core 消息当前
    # 无附件元数据,映射侧保持 None——契约预留,前端按字段渲染。
    attachments: list[Attachment] | None = None


class ToolApprovalRequest(ContractModel):
    """The exact validated invocation shown to the user before execution."""

    tool_call_id: str
    tool_name: str
    agent_role: AgentRole
    arguments: dict[str, Any]


class PendingToolApproval(ContractModel):
    """A tool invocation paused at a resumable graph gate."""

    interrupt_id: str
    request: ToolApprovalRequest


class PendingToolApprovalResponse(ContractModel):
    session_id: str
    pending_tool_approval: PendingToolApproval | None = None


class ToolApprovalDecisionAction(str, Enum):
    CONFIRM = "confirm"
    REJECT = "reject"


class ToolApprovalDecisionRequest(ContractModel):
    interrupt_id: str = Field(min_length=1)
    action: ToolApprovalDecisionAction

    @field_validator("interrupt_id")
    @classmethod
    def reject_blank_interrupt_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class RunEvent(ContractModel):
    """A replayable process event emitted during one run."""

    event_type: StreamEventType
    sequence: int = Field(ge=0)
    session_id: str | None = None
    run_id: str | None = None
    agent: AgentRole | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    parent_tool_call_id: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    content: str | None = None
    output_stream: Literal["stdout", "stderr"] | None = None
    message_id: str | None = None
    is_delta: bool | None = None
    success: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: ErrorCode | None = None
    plan_step_sequence: int | None = Field(default=None, ge=1)
    degraded: bool | None = None


class StreamEvent(ContractModel):
    """SSE 流式事件(D1-T1):基于 RunEvent 扩展内容字段。

    - tool_call / tool_result 仅携带 core 已有界、脱敏的输入/输出摘要;
    - thinking 是固定阶段文案，reasoning 才是 provider 显式返回的字段;
    - message_end 事件的 content 是最终消息全文(与 POST /chat 的
      ChatResponse.message.content 同源)。
    error_code 复用 RunError 的联合类型:流式 error 事件需要携带
    ApiErrorCode(SESSION_BUSY / INTERNAL_ERROR),仅 ErrorCode 装不下。
    """

    event_type: StreamEventType
    sequence: int = Field(ge=0)
    session_id: str
    run_id: str | None = None
    agent: AgentRole | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    parent_tool_call_id: str | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    success: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: ErrorCode | ApiErrorCode | None = None
    plan_step_sequence: int | None = Field(default=None, ge=1)
    content: str | None = None  # thinking 摘要 / message_delta 增量 / message_end 全文
    output_stream: Literal["stdout", "stderr"] | None = None
    message_id: str | None = None  # 同一模型消息的增量关联键
    is_delta: bool | None = None  # reasoning/message 的增量与完整快照标记
    message: Message | None = None  # message_end 的完整消息(可选)
    citations: list[Citation] | None = None
    current_agent: AgentRole | None = None
    pending_tool_approval: PendingToolApproval | None = None


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
    """Approval actions for one pending handoff interrupt."""

    CONFIRM = "confirm"
    REJECT = "reject"
    MODIFY = "modify"


class HandoffDecisionRequest(ContractModel):
    """A confirmation, rejection, or modification for one pending handoff interrupt."""

    interrupt_id: str = Field(min_length=1)
    action: HandoffDecisionAction
    target_agent: WorkerAgentRole | None = Field(
        default=None,
        description="The modified target worker; only valid when action is modify.",
    )
    task_content: str | None = Field(
        default=None,
        description="The modified task content; only valid when action is modify.",
    )

    @field_validator("interrupt_id")
    @classmethod
    def reject_blank_interrupt_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def action_matches_changes(self) -> HandoffDecisionRequest:
        """Replicate the core HandoffApprovalDecision change rule (both branches).

        - MODIFY must carry at least one of target_agent / task_content;
        - confirm / reject must not carry either modification field.
        Missing either branch would let invalid input reach the core
        HandoffApprovalDecision constructor and surface as a 500.
        """
        has_changes = self.target_agent is not None or self.task_content is not None
        if self.action is HandoffDecisionAction.MODIFY and not has_changes:
            raise ValueError("modify requires target_agent or task_content")
        if self.action is not HandoffDecisionAction.MODIFY and has_changes:
            raise ValueError("only modify accepts target_agent or task_content")
        return self

class Citation(ContractModel):
    """A safe reference placeholder for future retrieval-backed responses."""

    document_id: str
    source: str
    page: int | None = Field(default=None, ge=1)
    chunk_id: str


class KnowledgeSearchRequest(ContractModel):
    """知识库检索请求。

    top_k 必须由 API 层校验(Field ge/le)拦截在 422,不得依赖 core
    的 ValueError 运行时兜底(会变 500);query 的空白拦截与
    ChatRequest.reject_blank_text 同构(core 对空白 query 抛
    ValueError,同样要拦在 API 层)。
    """

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        """Reject whitespace-only queries before they reach the core service."""
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SearchHitDto(ContractModel):
    """检索命中的脱敏表示:chunk 摘要(截断)+ 逻辑 source 引用 + 分数。"""

    summary: str
    citation: Citation
    score: float


class KnowledgeSearchResponse(ContractModel):
    """检索结果(空库返回空 hits,不报错)。"""

    hits: list[SearchHitDto]


class KnowledgeDocumentListEntry(ContractModel):
    """知识库文档列表条目(只读元数据,不含内容)。

    page_count / chunk_count 可空:txt 无页概念、core 未来接入清单
    能力前由 API 层留空(见 api/knowledge.py 的 list_documents 注释)。
    """

    document_id: str
    source: str
    page_count: int | None = None
    chunk_count: int | None = None


class KnowledgeDocumentUploadResponse(ContractModel):
    """上传解析结果:文档已入库(幂等替换)后的元数据回执。"""

    document_id: str
    source: str
    page_count: int | None = None
    chunk_count: int | None = None


class KnowledgeDocumentListResponse(ContractModel):
    """文档清单响应(当前恒为空列表,原因见 list_documents 路由注释)。"""

    documents: list[KnowledgeDocumentListEntry]


class Attachment(ContractModel):
    """聊天消息附件引用(D7-T1 契约扩展预留)。

    - file_id / name / content_type / size 由上传回执(FILE-UPLOAD)填充;
    - 骨架期 chat 路由忽略该字段不影响现有行为(见 ChatRequest 注释),由
      D7-T3 或后续 core 能力决定如何进入模型上下文。
    """

    file_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=255)
    content_type: str | None = None
    size: int = Field(ge=0)


class FileUploadResponse(ContractModel):
    """文件上传回执(D7-T1):url 为受控下载的相对路径。

    - file_id 是服务端生成的 uuid4().hex + 白名单后缀(落盘名),url 形如
      /files/{file_id}——客户端凭 url 即可 GET 下载,url 不含原始文件名
      (后者只作展示字段 name);
    - content_type 由服务端按扩展名映射,不信任客户端伪造的类型;
    - name 是原始文件名(仅展示用;落盘名是 uuid,见 api/files.py 的
      防穿越设计)。
    """

    file_id: str
    name: str
    content_type: str | None
    size: int
    url: str


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
    run_id: str | None = None
    message: Message | None = None
    events: list[RunEvent] = Field(default_factory=list)
    run_error: RunError | None = None
    pending_handoff: PendingHandoff | None = None
    pending_tool_approval: PendingToolApproval | None = None
    references: list[Citation] | None = None
    task_plan: TaskPlan | None = None
    task_results: list[TaskResult] | None = None
    current_agent: AgentRole | None = None


class SessionProcess(ContractModel):
    """刷新或切回会话时用于重放协作过程的权威快照。"""

    run_id: str | None = None
    events: list[RunEvent] = Field(default_factory=list)
    task_plan: TaskPlan | None = None
    task_results: list[TaskResult] | None = None
    current_agent: AgentRole | None = None
    pending_tool_approval: PendingToolApproval | None = None


class FeedbackRating(str, Enum):
    """用户反馈评分方向。"""

    UP = "up"
    DOWN = "down"


class FeedbackRequest(ContractModel):
    """用户反馈请求体(只收脱敏引用字段,不收消息全文)。"""

    session_id: str = Field(max_length=200)
    message_id: str | None = Field(default=None, max_length=200)
    rating: FeedbackRating
    comment: str | None = Field(default=None, max_length=500)
    error_code: str | None = Field(default=None, max_length=100)


class FeedbackResponse(ContractModel):
    """反馈受理确认。"""

    received: bool = True


class StatsOverview(ContractModel):
    """学习进度基础统计(只读聚合,依赖既有 SessionStore/Graph 能力)。

    - agent_answer_counts 的键是 AgentRole 的字符串值(supervisor /
      teaching_assistant / learning_assistant / evaluator),口径与
      api/sessions._safe_agent 一致:识别不出角色的回答不计入任何键;
    - last_activity_at 为 ISO 时间戳,取当前用户所有会话 created_at
      的最大值(langchain-core 的 BaseMessage 无 created_at 字段,
      消息级时间不可用,见 api/stats.py 注释);无任何会话时为 None。
    """

    session_count: int
    message_count: int
    agent_answer_counts: dict[str, int]
    last_activity_at: str | None


CONTRACT_MODELS: tuple[type[ContractModel], ...] = (
    Session,
    CreateSessionRequest,
    ErrorDetail,
    ErrorResponse,
    ChatRequest,
    Message,
    ToolApprovalRequest,
    PendingToolApproval,
    PendingToolApprovalResponse,
    ToolApprovalDecisionRequest,
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
    SessionProcess,
    FeedbackRequest,
    FeedbackResponse,
    StatsOverview,
    KnowledgeSearchRequest,
    SearchHitDto,
    KnowledgeSearchResponse,
    KnowledgeDocumentListEntry,
    KnowledgeDocumentUploadResponse,
    KnowledgeDocumentListResponse,
    FileUploadResponse,
    Attachment,
)


def contract_openapi_schemas() -> dict[str, Any]:
    """Return Pydantic schemas for external models not yet used by a route."""
    _, schema = models_json_schema(
        [(model, "validation") for model in CONTRACT_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    return dict(schema["$defs"])
