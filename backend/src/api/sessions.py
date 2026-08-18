"""Session metadata and safe history REST routes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Annotated, Any, NoReturn, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from api.schemas import (
    AddWorkspaceRootRequest,
    AgentRole,
    ApiErrorCode,
    CreateSessionRequest,
    ErrorDetail,
    ErrorResponse,
    Message,
    MessageRole,
    Session,
    SessionProcess,
)
from core.events import EventType
from core.events import RunEvent as CoreRunEvent
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionRecord, SessionStore
from core.state import message_agent_role

router = APIRouter(prefix="/sessions", tags=["sessions"])
VALIDATION_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **VALIDATION_ERROR_RESPONSES,
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
}
LOOKUP_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **VALIDATION_ERROR_RESPONSES,
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
}


def current_user_id(
    x_user_id: Annotated[str | None, Header()] = None,
) -> str | None:
    """Return the optional user header without collapsing blank IDs into anonymous."""
    if x_user_id is not None and not x_user_id.strip():
        _raise_error(
            status.HTTP_400_BAD_REQUEST,
            ApiErrorCode.INVALID_REQUEST,
            "Request is invalid.",
        )
    return x_user_id


def _raise_error(status_code: int, error_code: ApiErrorCode, message: str) -> NoReturn:
    detail = ErrorDetail(error_code=error_code, message=message)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def _session_response(record: SessionRecord) -> Session:
    if record.workspace_root is None:
        raise RuntimeError("persisted session is missing its workspace root")
    return Session(
        session_id=record.session_id,
        user_id=record.user_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        archived=record.archived,
        title=record.title,
        workspace_root=record.workspace_root,
        additional_workspace_roots=list(record.additional_workspace_roots),
    )


def _owned_session(
    session_store: SessionStore, session_id: str, user_id: str | None
) -> SessionRecord | None:
    return session_store.get_session(session_id, user_id=user_id)


def _safe_agent(message: BaseMessage) -> AgentRole | None:
    """读取消息产出 Agent 角色：角色元数据优先，name 回退，最终降级为 None。"""

    # ── 第一步：优先读 core 的角色元数据 ────────────────────────────
    # core 提交 a6b31a3 之后，所有进入持久化历史的 AIMessage 都会经
    # core.state.with_agent_role 在 additional_kwargs["agent"] 中写入
    # 产出它的 Agent 角色，并随 checkpoint 序列化往返保留。这是刷新历史
    # 时判断「这条回答出自哪个 Agent」的稳定来源；而 message.name 在模型
    # 返回的 AIMessage 上从不被设置（现有实现因此恒为 null，前端只能显示
    # 「助手」降级徽章）。core 的读取函数 message_agent_role 是宽容读取：
    # 非助手消息 / 键缺失 / 值非法一律返回 None，不会因脏数据崩溃。
    role = message_agent_role(message)
    if role is not None:
        # core.state.AgentRole 与 api.schemas.AgentRole 的字符串值完全
        # 一致（supervisor / teaching_assistant / learning_assistant /
        # evaluator），这里只做值透传，不重复定义映射。
        try:
            return AgentRole(role.value)
        except ValueError:
            # ── 防御：两份枚举是独立定义 ──────────────────────────
            # core 与 api 的 AgentRole 是各自维护的枚举。若未来 core
            # 新增角色成员而 api 未同步，message_agent_role 会返回一个
            # core 合法、api 无法构造的值，硬构造将抛未捕获 ValueError
            # 变成 HTTP 500。这里与 name 回退的降级语义对齐：返回 None，
            # 让前端显示「助手」徽章，而不是让异常击穿接口。
            return None

    # ── 第二步：name 回退，兼容旧 checkpoint 数据 ──────────────────
    # a6b31a3 之前持久化的历史消息没有角色元数据；老实现从 message.name
    # 读取角色（少数手工构造的消息会设置 name）。保留该回退路径，让旧会话
    # 刷新后仍能显示具体 Agent 角色，而不是一律降级。
    name = message.name
    if not isinstance(name, str):
        return None
    try:
        return AgentRole(name)
    except ValueError:
        # ── 降级语义 ──────────────────────────────────────────────
        # 元数据缺失且 name 非法（脏数据）时返回 None：接口不报错，
        # 前端据此显示「助手」降级徽章，保证任何历史数据都能正常渲染。
        return None


def _safe_created_at(message: BaseMessage) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    return created_at if isinstance(created_at, datetime) else None


def _public_message(message: BaseMessage, user_id: str | None = None) -> Message | None:
    if isinstance(message, HumanMessage):
        role = MessageRole.USER
    elif isinstance(message, AIMessage) and not message.tool_calls:
        role = MessageRole.ASSISTANT
    else:
        return None

    if not isinstance(message.content, str):
        return None
    # 延迟导入避免模块环：api/files.py 在模块顶部从本模块导入
    # current_user_id（与 get_session_process 延迟导入 api.chat 同一模式）。
    from api.files import attachments_for_generated_files

    return Message(
        role=role,
        content=message.content,
        agent=_safe_agent(message),
        created_at=_safe_created_at(message),
        # T5-3:core 助手消息可能携带 officecli 生成文件元数据,这里注册为
        # 受控下载附件后透传;无元数据时返回 None,前端零渲染(与 D7-T3
        # 预留语义一致)。
        attachments=attachments_for_generated_files(user_id, message),
    )


def _public_messages(
    messages: Iterable[BaseMessage],
    user_id: str | None = None,
) -> list[Message]:
    return [
        public_message
        for message in messages
        if (public_message := _public_message(message, user_id))
    ]


def _latest_run_events(state: Mapping[str, Any]) -> list[object]:
    """Return one execution turn, with a boundary fallback for legacy checkpoints."""
    events = list(state.get("events", []))
    run_id = state.get("run_id")
    if isinstance(run_id, str) and run_id:
        return [
            event
            for event in events
            if isinstance(event, CoreRunEvent) and event.run_id == run_id
        ]

    # 旧 checkpoint 没有 run_id。用终态事件切出最后一轮，避免升级后首次
    # 刷新仍把多轮执行记录混在一起；没有终态边界时保留全部以避免丢数据。
    terminal_indexes = [
        index
        for index, event in enumerate(events)
        if isinstance(event, CoreRunEvent)
        and event.event_type in {EventType.RUN_COMPLETED, EventType.RUN_FAILED}
    ]
    if not terminal_indexes:
        return events
    if terminal_indexes[-1] == len(events) - 1:
        boundary = terminal_indexes[-2] if len(terminal_indexes) > 1 else -1
    else:
        boundary = terminal_indexes[-1]
    return events[boundary + 1 :]


@router.post(
    "",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
def create_session(
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
    payload: CreateSessionRequest | None = None,
) -> Session:
    """Create a session within the current user's isolated namespace."""
    session_id = payload.session_id if payload and payload.session_id is not None else str(uuid4())
    if not session_id.strip():
        _raise_error(
            status.HTTP_400_BAD_REQUEST,
            ApiErrorCode.INVALID_REQUEST,
            "Request is invalid.",
        )
    session_store = _session_store(request)
    requested_workspace = payload.workspace_root if payload is not None else None
    try:
        workspace_root = session_store.resolve_workspace_root(requested_workspace)
    except ValueError:
        _raise_error(
            status.HTTP_400_BAD_REQUEST,
            ApiErrorCode.INVALID_REQUEST,
            "Workspace directory is invalid or not allowed.",
        )
    try:
        record = session_store.create_session(
            session_id,
            user_id=user_id,
            workspace_root=workspace_root,
        )
    except ValueError:
        _raise_error(
            status.HTTP_409_CONFLICT,
            ApiErrorCode.SESSION_ALREADY_EXISTS,
            "Session already exists.",
        )
    return _session_response(record)


@router.post(
    "/{session_id}/workspace-roots",
    response_model=Session,
    responses=LOOKUP_ERROR_RESPONSES,
)
def add_workspace_root(
    session_id: str,
    payload: AddWorkspaceRootRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> Session:
    """Authorize one additional directory for this session's file tools."""
    session_store = _session_store(request)
    try:
        record = session_store.add_workspace_root(
            session_id,
            payload.path,
            user_id=user_id,
        )
    except ValueError:
        _raise_error(
            status.HTTP_400_BAD_REQUEST,
            ApiErrorCode.INVALID_REQUEST,
            "Workspace directory is invalid or not allowed.",
        )
    if record is None:
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )
    return _session_response(record)


@router.get(
    "",
    response_model=list[Session],
    responses=VALIDATION_ERROR_RESPONSES,
)
def list_sessions(
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
    include_archived: bool = False,
) -> list[Session]:
    """List only the current user's sessions."""
    records = _session_store(request).list_sessions(
        user_id=user_id,
        include_archived=include_archived,
    )
    return [_session_response(record) for record in records]


@router.post(
    "/{session_id}/archive",
    response_model=Session,
    responses=LOOKUP_ERROR_RESPONSES,
)
def archive_session(
    session_id: str,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> Session:
    """Archive an active session without revealing another user's session."""
    session_store = _session_store(request)
    if not session_store.archive_session(session_id, user_id=user_id):
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )
    record = _owned_session(session_store, session_id, user_id)
    if record is None:
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )
    return _session_response(record)


@router.get(
    "/{session_id}/messages",
    response_model=list[Message],
    responses=LOOKUP_ERROR_RESPONSES,
)
def get_session_history(
    session_id: str,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> list[Message]:
    """Return only safe history fields for the current user's session."""
    if _owned_session(_session_store(request), session_id, user_id) is None:
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )
    return _public_messages(
        _graph(request).get_history(session_id, user_id=user_id),
        user_id,
    )


@router.get(
    "/{session_id}/process",
    response_model=SessionProcess,
    responses=LOOKUP_ERROR_RESPONSES,
)
def get_session_process(
    session_id: str,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> SessionProcess:
    """返回 checkpoint 中可回放的思考、工具与协作过程快照。"""
    if _owned_session(_session_store(request), session_id, user_id) is None:
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )

    # 延迟导入避免 chat.py -> sessions.py(current_user_id) 的模块环。
    from api.chat import (
        _public_agent,
        _public_events,
        _public_pending_tool_approval,
        _public_task_plan,
        _public_task_results,
    )

    graph = _graph(request)
    state = graph.get_state(session_id, user_id=user_id)
    if state is None:
        return SessionProcess()
    pending_method = getattr(graph, "get_pending_tool_approval", None)
    pending_tool_approval = (
        _public_pending_tool_approval(pending_method(session_id, user_id))
        if callable(pending_method)
        else None
    )
    return SessionProcess(
        run_id=state.get("run_id"),
        events=_public_events(_latest_run_events(state), -1),
        task_plan=_public_task_plan(state.get("task_plan")),
        task_results=_public_task_results(state.get("task_results")),
        current_agent=_public_agent(state.get("current_agent")),
        pending_tool_approval=pending_tool_approval,
    )
