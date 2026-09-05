"""Approved, workspace-scoped shell execution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.filesystem import WorkspaceFileSystem, workspace_scope
from core.tools.shell_tool import (
    approved_shell_execution,
    create_shell_tool,
    shell_output_scope,
)


def test_shell_requires_an_explicit_approval_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shell = create_shell_tool(WorkspaceFileSystem(workspace))

    with workspace_scope(workspace), pytest.raises(PermissionError, match="approval"):
        shell.invoke({"command": "echo blocked", "cwd": "."})


def test_approved_shell_runs_a_compound_command_and_streams_output(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    chunks: list[tuple[str, str]] = []
    shell = create_shell_tool(WorkspaceFileSystem(workspace))

    with (
        workspace_scope(workspace),
        approved_shell_execution(),
        shell_output_scope(lambda channel, text: chunks.append((channel, text))),
    ):
        result = shell.invoke(
            {
                "command": "echo first; echo second",
                "cwd": ".",
                "description": "print two lines",
                "timeout_seconds": 10,
            }
        )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "first" in result["stdout"]
    assert "second" in result["stdout"]
    assert result["cwd"] == str(workspace.resolve())
    assert "first" in "".join(text for channel, text in chunks if channel == "stdout")


def test_shell_rejects_a_working_directory_outside_authorized_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    shell = create_shell_tool(WorkspaceFileSystem(workspace))

    with (
        workspace_scope(workspace),
        approved_shell_execution(),
        pytest.raises(RuntimeError, match="授权"),
    ):
        shell.invoke({"command": "echo blocked", "cwd": str(outside)})
