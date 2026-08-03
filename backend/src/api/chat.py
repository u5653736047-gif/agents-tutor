"""Synchronous chat REST route backed by the collaborative graph."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, status
from langchain_core.messages import AIMessage, BaseMessage
from starlette.concurrency import run_in_threadpool

from api.schemas import (
    AgentRole,
    ApiErrorCode,
    ChatRequest,
    ChatResponse,
    ErrorResponse,
    HandoffRequest,
    Message,
    MessageRole,
    PendingHandoff,
    RunError,
    RunEvent,
    StreamEventType,
    WorkerAgentRole,
)
from api.schemas import (
    ErrorCode as ApiRunErrorCode,
)
from api.sessions import current_user_id
from core.events import EventType
from core.events import RunError as CoreRunError
from core.events import RunEvent as CoreRunEvent
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionStore
from core.state import AgentState, PendingHandoffApproval

router = APIRouter(tags=["chat"])
CHAT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}
EVENT_TYPE_MAP = {
    EventType.AGENT_STARTED: StreamEventType.THINKING,
    EventType.TOOL_STARTED: StreamEventType.TOOL_CALL,
    EventType.TOOL_COMPLETED: StreamEventType.TOOL_RESULT,
    EventType.AGENT_COMPLETED: StreamEventType.MESSAGE_END,
    EventType.AGENT_SWITCHED: StreamEventType.AGENT_SWITCH,
    EventType.RUN_FAILED: StreamEventType.ERROR,
    EventType.RUN_COMPLETED: StreamEventType.DONE,
}
PENDING_RESUME_ERROR_PREFIX = "存在待恢复执行，请先调用 "


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def session_lock(
    request: Request, session_id: str, user_id: str | None
) -> asyncio.Lock:
    locks = cast(
        dict[tuple[str | None, str], asyncio.Lock], request.app.state.chat_session_locks
    )
    key = (user_id, session_id)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _public_agent(value: object) -> AgentRole | None:
    if not isinstance(value, str):
        return None
    try:
        return AgentRole(value)
    except ValueError:
        return None


def _safe_created_at(message: BaseMessage) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    return created_at if isinstance(created_at, datetime) else None


def _final_assistant_message(
    state: AgentState, previous_message_count: int
) -> Message | None:
    agent = _public_agent(state.get("current_agent"))
    messages = state.get("messages", [])
    for message in reversed(messages[previous_message_count:]):
        if (
            isinstance(message, AIMessage)
            and not message.tool_calls
            and isinstance(message.content, str)
        ):
            return Message(
                role=MessageRole.ASSISTANT,
                content=message.content,
                agent=agent,
                created_at=_safe_created_at(message),
            )
    return None


def _previous_sequence(state: AgentState | None) -> int:
    if state is None:
        return -1
    return max(
        (
            event.sequence
            for event in state.get("events", [])
            if isinstance(event, CoreRunEvent)
        ),
        default=-1,
    )


def _previous_message_count(state: AgentState | None) -> int:
    return 0 if state is None else len(state.get("messages", []))


def _public_event(event: CoreRunEvent) -> RunEvent | None:
    event_type = EVENT_TYPE_MAP.get(event.event_type)
    if event_type is None:
        return None
    error_code = None
    if event.error_code is not None:
        error_code = ApiRunErrorCode(event.error_code.value)
    return RunEvent(
        event_type=event_type,
        sequence=event.sequence,
        session_id=event.session_id,
        agent=_public_agent(event.agent),
        tool_name=event.tool_name,
        success=event.success,
        duration_ms=event.duration_ms,
        error_code=error_code,
        plan_step_sequence=event.plan_step_sequence,
        degraded=event.degraded,
    )


def _public_events(events: Iterable[object], sequence: int) -> list[RunEvent]:
    return [
        public_event
        for event in events
        if isinstance(event, CoreRunEvent)
        and event.sequence > sequence
        and (public_event := _public_event(event)) is not None
    ]


def _public_run_error(error: object) -> RunError | None:
    if not isinstance(error, CoreRunError):
        return None
    return RunError(
        error_code=ApiRunErrorCode(error.error_code.value),
        message="The request could not be completed.",
        agent=_public_agent(error.agent),
    )


def _public_pending_handoff(pending: object) -> PendingHandoff | None:
    if not isinstance(pending, PendingHandoffApproval):
        return None
    return PendingHandoff(
        interrupt_id=pending.interrupt_id,
        request=HandoffRequest(
            target_agent=WorkerAgentRole(pending.request.target_agent.value),
            task_content=pending.request.task_content,
            plan_step_sequence=pending.request.plan_step_sequence,
        ),
    )


def _ensure_session(session_store: SessionStore, session_id: str, user_id: str | None) -> None:
    if any(
        record.session_id == session_id
        for record in session_store.list_sessions(user_id=user_id, include_archived=True)
    ):
        return
    try:
        session_store.create_session(session_id, user_id=user_id)
    except ValueError:
        if not any(
            record.session_id == session_id
            for record in session_store.list_sessions(user_id=user_id, include_archived=True)
        ):
            raise


async def pending_handoff_for_session(
    graph: CollaborativeAgentGraph, session_id: str, user_id: str | None
) -> PendingHandoff | None:
    pending = await run_in_threadpool(graph.get_pending_handoff, session_id, user_id)
    return _public_pending_handoff(pending)


def session_busy_response(session_id: str, message: str) -> ChatResponse:
    return ChatResponse(
        session_id=session_id,
        run_error=RunError(
            error_code=ApiErrorCode.SESSION_BUSY,
            message=message,
        ),
    )


async def chat_response_for_state(
    graph: CollaborativeAgentGraph,
    state: AgentState,
    session_id: str,
    user_id: str | None,
    previous_state: AgentState | None,
) -> ChatResponse:
    """Convert one completed graph transition into the public chat contract."""
    return ChatResponse(
        session_id=session_id,
        message=_final_assistant_message(state, _previous_message_count(previous_state)),
        events=_public_events(state.get("events", []), _previous_sequence(previous_state)),
        run_error=_public_run_error(state.get("run_error")),
        pending_handoff=await pending_handoff_for_session(graph, session_id, user_id),
        current_agent=_public_agent(state.get("current_agent")),
    )


@router.post("/chat", response_model=ChatResponse, responses=CHAT_ERROR_RESPONSES)
async def chat(
    payload: ChatRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> ChatResponse:
    """Run one synchronous collaboration turn in a worker thread."""
    graph = _graph(request)
    session_store = _session_store(request)
    active_session_lock = session_lock(request, payload.session_id, user_id)
    if active_session_lock.locked():
        return session_busy_response(
            payload.session_id,
            "Another request is already running for this session.",
        )

    async with active_session_lock:
        await run_in_threadpool(_ensure_session, session_store, payload.session_id, user_id)
        previous_state = await run_in_threadpool(
            graph.get_state, payload.session_id, user_id
        )
        try:
            state = await run_in_threadpool(
                graph.run, payload.message, payload.session_id, user_id
            )
        except RuntimeError as error:
            pending_handoff = await pending_handoff_for_session(
                graph, payload.session_id, user_id
            )
            if pending_handoff is not None or str(error).startswith(
                PENDING_RESUME_ERROR_PREFIX
            ):
                return ChatResponse(
                    session_id=payload.session_id,
                    run_error=RunError(
                        error_code=ApiErrorCode.SESSION_BUSY,
                        message="The session is waiting for a pending operation.",
                    ),
                    pending_handoff=pending_handoff,
                )
            return ChatResponse(
                session_id=payload.session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )
        except Exception:  # noqa: BLE001 - graph boundary exposes only stable error data
            return ChatResponse(
                session_id=payload.session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )

        return await chat_response_for_state(
            graph,
            state,
            payload.session_id,
            user_id,
            previous_state,
        )
