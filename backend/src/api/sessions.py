"""Session metadata and safe history REST routes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, NoReturn, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from api.schemas import (
    AgentRole,
    ApiErrorCode,
    CreateSessionRequest,
    ErrorDetail,
    ErrorResponse,
    Message,
    MessageRole,
    Session,
)
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionRecord, SessionStore

router = APIRouter(prefix="/sessions", tags=["sessions"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
}
LOOKUP_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
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
    return Session(
        session_id=record.session_id,
        user_id=record.user_id,
        created_at=record.created_at,
        archived=record.archived,
    )


def _owned_session(
    session_store: SessionStore, session_id: str, user_id: str | None
) -> SessionRecord | None:
    return next(
        (
            record
            for record in session_store.list_sessions(user_id=user_id, include_archived=True)
            if record.session_id == session_id
        ),
        None,
    )


def _safe_agent(message: BaseMessage) -> AgentRole | None:
    name = message.name
    if not isinstance(name, str):
        return None
    try:
        return AgentRole(name)
    except ValueError:
        return None


def _safe_created_at(message: BaseMessage) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    return created_at if isinstance(created_at, datetime) else None


def _public_message(message: BaseMessage) -> Message | None:
    if isinstance(message, HumanMessage):
        role = MessageRole.USER
    elif isinstance(message, AIMessage) and not message.tool_calls:
        role = MessageRole.ASSISTANT
    else:
        return None

    if not isinstance(message.content, str):
        return None
    return Message(
        role=role,
        content=message.content,
        agent=_safe_agent(message),
        created_at=_safe_created_at(message),
    )


def _public_messages(messages: Iterable[BaseMessage]) -> list[Message]:
    return [public_message for message in messages if (public_message := _public_message(message))]


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
    try:
        record = _session_store(request).create_session(session_id, user_id=user_id)
    except ValueError:
        _raise_error(
            status.HTTP_409_CONFLICT,
            ApiErrorCode.SESSION_ALREADY_EXISTS,
            "Session already exists.",
        )
    return _session_response(record)


@router.get("", response_model=list[Session])
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
    return _public_messages(_graph(request).get_history(session_id, user_id=user_id))
