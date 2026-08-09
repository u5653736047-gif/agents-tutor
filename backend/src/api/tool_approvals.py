"""REST and SSE boundaries for approval-gated tool invocations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from api.chat import (
    chat_response_for_state,
    pending_tool_approval_for_session,
    session_busy_response,
    session_lock,
)
from api.schemas import (
    ApiErrorCode,
    ChatResponse,
    ErrorDetail,
    ErrorResponse,
    PendingToolApprovalResponse,
    RunError,
    ToolApprovalDecisionRequest,
)
from api.sessions import current_user_id
from api.stream import tool_approval_stream_events
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionStore
from core.state import ToolApprovalAction, ToolApprovalDecision

router = APIRouter(prefix="/sessions", tags=["tool-approvals"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def _owns_session(
    session_store: SessionStore,
    session_id: str,
    user_id: str | None,
) -> bool:
    return any(
        record.session_id == session_id
        for record in session_store.list_sessions(
            user_id=user_id,
            include_archived=True,
        )
    )


def _raise_error(
    status_code: int,
    error_code: ApiErrorCode,
    message: str,
) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail=ErrorDetail(
            error_code=error_code,
            message=message,
        ).model_dump(mode="json"),
    )


async def _require_owned_session(
    session_store: SessionStore,
    session_id: str,
    user_id: str | None,
) -> None:
    if not await run_in_threadpool(
        _owns_session,
        session_store,
        session_id,
        user_id,
    ):
        _raise_error(
            status.HTTP_404_NOT_FOUND,
            ApiErrorCode.SESSION_NOT_FOUND,
            "Session was not found.",
        )


def _raise_tool_approval_not_pending() -> NoReturn:
    _raise_error(
        status.HTTP_409_CONFLICT,
        ApiErrorCode.TOOL_APPROVAL_NOT_PENDING,
        "No matching tool approval is pending for this session.",
    )


@router.get(
    "/{session_id}/tool-approval",
    response_model=PendingToolApprovalResponse,
    responses=ERROR_RESPONSES,
)
async def get_pending_tool_approval(
    session_id: str,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> PendingToolApprovalResponse:
    """Return the exact validated invocation currently awaiting consent."""
    active_session_lock = session_lock(request, session_id, user_id)
    async with active_session_lock:
        await _require_owned_session(_session_store(request), session_id, user_id)
        return PendingToolApprovalResponse(
            session_id=session_id,
            pending_tool_approval=await pending_tool_approval_for_session(
                _graph(request),
                session_id,
                user_id,
            ),
        )


@router.post(
    "/{session_id}/tool-approval",
    response_model=ChatResponse,
    responses=ERROR_RESPONSES,
)
async def decide_tool_approval(
    session_id: str,
    payload: ToolApprovalDecisionRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> ChatResponse:
    """Confirm or reject one exact invocation, then finish the graph turn."""
    graph = _graph(request)
    active_session_lock = session_lock(request, session_id, user_id)
    if active_session_lock.locked():
        return session_busy_response(
            session_id,
            "Another request is already running for this session.",
        )

    async with active_session_lock:
        await _require_owned_session(_session_store(request), session_id, user_id)
        pending = await pending_tool_approval_for_session(
            graph,
            session_id,
            user_id,
        )
        if pending is None or pending.interrupt_id != payload.interrupt_id:
            _raise_tool_approval_not_pending()

        previous_state = await run_in_threadpool(
            graph.get_state,
            session_id,
            user_id,
        )
        decision = ToolApprovalDecision(
            interrupt_id=payload.interrupt_id,
            action=ToolApprovalAction(payload.action.value),
        )
        try:
            state = await run_in_threadpool(
                graph.resume_tool_approval,
                session_id,
                decision,
                user_id,
            )
        except ValueError:
            _raise_tool_approval_not_pending()
        except Exception:  # noqa: BLE001 - stable errors only at API boundary
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


@router.post(
    "/{session_id}/tool-approval/stream",
    responses=ERROR_RESPONSES,
)
async def stream_tool_approval(
    session_id: str,
    payload: ToolApprovalDecisionRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> Response:
    """Resume one exact tool gate and stream terminal/model output as SSE."""
    graph = _graph(request)
    active_session_lock = session_lock(request, session_id, user_id)
    if active_session_lock.locked():
        return JSONResponse(
            content=session_busy_response(
                session_id,
                "Another request is already running for this session.",
            ).model_dump(mode="json"),
            status_code=200,
        )

    await active_session_lock.acquire()
    try:
        await _require_owned_session(_session_store(request), session_id, user_id)
        pending = await pending_tool_approval_for_session(
            graph,
            session_id,
            user_id,
        )
        if pending is None or pending.interrupt_id != payload.interrupt_id:
            _raise_tool_approval_not_pending()
        previous_state = await run_in_threadpool(
            graph.get_state,
            session_id,
            user_id,
        )
        decision = ToolApprovalDecision(
            interrupt_id=payload.interrupt_id,
            action=ToolApprovalAction(payload.action.value),
        )
    except BaseException:
        active_session_lock.release()
        raise

    async def locked_frames() -> AsyncIterator[str]:
        try:
            async for frame in tool_approval_stream_events(
                graph,
                session_id,
                decision,
                request,
                user_id,
                previous_state,
            ):
                yield frame
        finally:
            active_session_lock.release()

    return StreamingResponse(
        locked_frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
