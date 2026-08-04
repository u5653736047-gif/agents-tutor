"""Minimal REST routes for pending handoff approval."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from api.chat import (
    chat_response_for_state,
    pending_handoff_for_session,
    session_busy_response,
    session_lock,
)
from api.schemas import (
    ApiErrorCode,
    ChatResponse,
    ErrorDetail,
    ErrorResponse,
    HandoffDecisionRequest,
    PendingHandoffResponse,
    RunError,
)
from api.sessions import current_user_id
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionStore
from core.state import AgentRole, HandoffApprovalAction, HandoffApprovalDecision

router = APIRouter(prefix="/sessions", tags=["handoffs"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def _owns_session(session_store: SessionStore, session_id: str, user_id: str | None) -> bool:
    return any(
        record.session_id == session_id
        for record in session_store.list_sessions(user_id=user_id, include_archived=True)
    )


def _raise_error(status_code: int, error_code: ApiErrorCode, message: str) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail=ErrorDetail(error_code=error_code, message=message).model_dump(mode="json"),
    )


async def _require_owned_session(
    session_store: SessionStore, session_id: str, user_id: str | None
) -> None:
    if not await run_in_threadpool(_owns_session, session_store, session_id, user_id):
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )


def _raise_handoff_not_pending() -> NoReturn:
    _raise_error(
        status.HTTP_409_CONFLICT,
        ApiErrorCode.HANDOFF_NOT_PENDING,
        "No handoff is pending for this session.",
    )


@router.get(
    "/{session_id}/handoff",
    response_model=PendingHandoffResponse,
    responses=ERROR_RESPONSES,
)
async def get_pending_handoff(
    session_id: str,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> PendingHandoffResponse:
    """Return the current user's pending handoff, if any."""
    active_session_lock = session_lock(request, session_id, user_id)
    async with active_session_lock:
        await _require_owned_session(_session_store(request), session_id, user_id)
        return PendingHandoffResponse(
            session_id=session_id,
            pending_handoff=await pending_handoff_for_session(
                _graph(request), session_id, user_id
            ),
        )


@router.post(
    "/{session_id}/handoff",
    response_model=ChatResponse,
    responses=ERROR_RESPONSES,
)
async def decide_handoff(
    session_id: str,
    payload: HandoffDecisionRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> ChatResponse:
    """Confirm or reject the current handoff, then return its graph transition."""
    graph = _graph(request)
    active_session_lock = session_lock(request, session_id, user_id)
    if active_session_lock.locked():
        return session_busy_response(
            session_id,
            "Another request is already running for this session.",
        )

    async with active_session_lock:
        await _require_owned_session(_session_store(request), session_id, user_id)
        pending_handoff = await pending_handoff_for_session(graph, session_id, user_id)
        if pending_handoff is None or pending_handoff.interrupt_id != payload.interrupt_id:
            _raise_handoff_not_pending()

        previous_state = await run_in_threadpool(graph.get_state, session_id, user_id)
        # D2-T4:构造 core 决策时透传修改字段。API 层双分支校验(见
        # HandoffDecisionRequest.action_matches_changes)已挡非法组合;此处再
        # 兜底一次——core 侧还有独立校验(如空白 task_content 会被拒),抛出的
        # ValueError 统一转 422,防止校验异常穿透成 500。
        try:
            decision = HandoffApprovalDecision(
                interrupt_id=payload.interrupt_id,
                action=HandoffApprovalAction(payload.action.value),
                # api WorkerAgentRole 与 core AgentRole 值字符串一致,按值显式
                # 转换(WorkerAgentRole 不含 supervisor,天然满足 core 校验)。
                target_agent=(
                    AgentRole(payload.target_agent.value)
                    if payload.target_agent is not None
                    else None
                ),
                task_content=payload.task_content,
            )
        except ValueError:
            _raise_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ApiErrorCode.INVALID_REQUEST,
                "Request is invalid.",
            )
        try:
            state = await run_in_threadpool(
                graph.resume_handoff,
                session_id,
                decision,
                user_id,
            )
        except ValueError:
            _raise_handoff_not_pending()
        except Exception:  # noqa: BLE001 - only stable errors cross the API boundary
            return ChatResponse(
                session_id=session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )

        return await chat_response_for_state(
            graph,
            state,
            session_id,
            user_id,
            previous_state,
        )
