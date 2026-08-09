"""Workspace-scoped filesystem capabilities used by agent tools."""

from .workspace import (
    WorkspaceFileError,
    WorkspaceFileErrorCode,
    WorkspaceFileSystem,
    active_workspace_filesystem,
    workspace_scope,
)

__all__ = [
    "WorkspaceFileError",
    "WorkspaceFileErrorCode",
    "WorkspaceFileSystem",
    "active_workspace_filesystem",
    "workspace_scope",
]
