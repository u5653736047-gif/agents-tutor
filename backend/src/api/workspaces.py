"""Server-side directory validation and browsing for local workspaces."""

from __future__ import annotations

from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.schemas import (
    AddWorkspaceRootRequest,
    ApiErrorCode,
    ErrorDetail,
    ErrorResponse,
    WorkspaceDirectoryListing,
    WorkspacePath,
)
from api.sessions import current_user_id
from core.sessions import SessionStore

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _invalid_workspace() -> NoReturn:
    detail = ErrorDetail(
        error_code=ApiErrorCode.INVALID_REQUEST,
        message="Workspace directory is invalid or not allowed.",
    )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=detail.model_dump(mode="json"),
    )


@router.post(
    "/validate",
    response_model=WorkspacePath,
    responses=ERROR_RESPONSES,
)
def validate_workspace(
    payload: AddWorkspaceRootRequest,
    request: Request,
    _user_id: Annotated[str | None, Depends(current_user_id)],
) -> WorkspacePath:
    """Resolve a typed path before the user creates or updates a session."""
    try:
        path = _session_store(request).resolve_workspace_root(payload.path)
    except ValueError:
        _invalid_workspace()
    name = path.rstrip("/\\").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    return WorkspacePath(path=path, name=name or path)


@router.get(
    "/directories",
    response_model=WorkspaceDirectoryListing,
    responses=ERROR_RESPONSES,
)
def list_workspace_directories(
    request: Request,
    _user_id: Annotated[str | None, Depends(current_user_id)],
    path: Annotated[str | None, Query(min_length=1)] = None,
) -> WorkspaceDirectoryListing:
    """Browse immediate directories without exposing files or file contents."""
    try:
        listing = _session_store(request).list_workspace_directories(path)
    except ValueError:
        _invalid_workspace()
    return WorkspaceDirectoryListing.model_validate(listing)


__all__ = ["router"]
