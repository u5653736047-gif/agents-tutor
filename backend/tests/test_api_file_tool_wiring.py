"""Production lifespan wiring for workspace-scoped read-only file tools."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from api.app import create_app
from core.state import AgentRole

_FILE_TOOL_NAMES = (
    "workspace_info",
    "list_files",
    "glob_files",
    "grep_files",
    "read_file",
    "inspect_workspace",
)


def _configure_runtime(monkeypatch: MonkeyPatch, tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "lesson.txt").write_text("workspace lesson\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    monkeypatch.setenv("API_KNOWLEDGE_DB_PATH", str(tmp_path / "knowledge.sqlite3"))
    monkeypatch.setenv("API_VECTOR_DB_PATH", str(tmp_path / "missing-vector.sqlite3"))
    monkeypatch.setenv("API_KNOWLEDGE_EMBEDDING", "hash")
    monkeypatch.setenv("API_WORKSPACE_ROOT", str(workspace))
    return workspace


def test_lifespan_wires_read_only_tools_to_configured_workspace(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _configure_runtime(monkeypatch, tmp_path)
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            graph = app.state.graph
            for tool_name in _FILE_TOOL_NAMES:
                tool = graph.registry.get(tool_name)
                assert tool is not None
                assert tool.extras == {"category": "filesystem", "read_only": True}
                assert graph.registry.is_authorized(tool_name, AgentRole.SUPERVISOR)
                assert graph.registry.is_authorized(tool_name, AgentRole.TEACHING_ASSISTANT)
                assert graph.registry.is_authorized(tool_name, AgentRole.LEARNING_ASSISTANT)
                assert not graph.registry.is_authorized(tool_name, AgentRole.EVALUATOR)

            read_tool = graph.registry.get("read_file")
            assert read_tool is not None
            result = read_tool.invoke({"path": "lesson.txt"})
            assert result["content"] == "1: workspace lesson"

            info_tool = graph.registry.get("workspace_info")
            assert info_tool is not None
            workspace_info = info_tool.invoke({})
            assert workspace_info["workspace_name"] == "workspace"
            assert workspace_info["root"] == str(workspace.resolve())
            assert workspace_info["additional_roots"] == []
            assert workspace_info["access"] == "read_only"
            assert graph.registry.get("write_file") is None
            assert graph.registry.get("edit_file") is None

            shell_tool = graph.registry.get("shell")
            assert shell_tool is not None
            assert shell_tool.extras == {
                "category": "terminal",
                "requires_approval": True,
                "status_from_ok": True,
            }
            assert graph.registry.is_authorized("shell", AgentRole.SUPERVISOR)
            assert not graph.registry.is_authorized(
                "shell", AgentRole.TEACHING_ASSISTANT
            )
            with pytest.raises(PermissionError, match="explicit user approval"):
                shell_tool.invoke({"command": "echo blocked"})

    asyncio.run(verify_runtime())


def test_lifespan_uses_workspace_configuration_for_new_sessions(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _configure_runtime(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("API_WORKSPACE_ALLOWED_ROOTS", str(workspace))
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            store = app.state.session_store
            default_session = store.create_session("default", user_id="user-1")
            assert default_session.workspace_root == str(workspace.resolve())
            with pytest.raises(ValueError, match="not allowed"):
                store.create_session(
                    "outside",
                    user_id="user-1",
                    workspace_root=outside,
                )

    asyncio.run(verify_runtime())


def test_lifespan_wires_office_tools_when_enabled(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ENABLED=1：office 工具对按 3.9 权限矩阵注册，双层超时按 +5s 推导。

    用 sys.executable 充当假二进制（--version 自检可真实运行，版本不一致
    仅告警），覆盖开启态的完整装配路径。
    """
    import sys

    _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("API_OFFICECLI_ENABLED", "1")
    monkeypatch.setenv("API_OFFICECLI_BINARY", sys.executable)
    monkeypatch.setenv("API_OFFICECLI_TIMEOUT_READ_SECONDS", "60")
    monkeypatch.setenv("API_OFFICECLI_TIMEOUT_WRITE_SECONDS", "120")
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            graph = app.state.graph
            inspect_tool = graph.registry.get("officecli_inspect")
            edit_tool = graph.registry.get("officecli_edit")
            assert inspect_tool is not None and edit_tool is not None
            assert inspect_tool.extras == {"category": "office", "read_only": True}
            assert edit_tool.extras == {
                "category": "office",
                "requires_approval": True,
                "status_from_ok": True,
            }
            # 3.9 权限矩阵：inspect 四角色可用；edit 不授给助学
            for role in AgentRole:
                assert graph.registry.is_authorized("officecli_inspect", role)
            assert graph.registry.is_authorized("officecli_edit", AgentRole.SUPERVISOR)
            assert graph.registry.is_authorized(
                "officecli_edit", AgentRole.TEACHING_ASSISTANT
            )
            assert graph.registry.is_authorized("officecli_edit", AgentRole.EVALUATOR)
            assert not graph.registry.is_authorized(
                "officecli_edit", AgentRole.LEARNING_ASSISTANT
            )
            # 双层超时推导：执行器时限 = 子进程超时 + 5
            executor = graph.agents[AgentRole.SUPERVISOR].tool_executor
            assert executor.timeout_seconds_for("officecli_inspect") == 65
            assert executor.timeout_seconds_for("officecli_edit") == 125
            # 写工具运行时门：未批准上下文直接调用必须被拒
            with pytest.raises(PermissionError, match="approval"):
                edit_tool.invoke({"command": ["create", "x.xlsx"]})

    asyncio.run(verify_runtime())


def test_lifespan_omits_office_tools_when_disabled(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """ENABLED 缺省（0）：工具与权限声明同步缺席，graph 权限校验不受影响。"""
    _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("API_OFFICECLI_ENABLED", raising=False)
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            graph = app.state.graph
            assert graph.registry.get("officecli_inspect") is None
            assert graph.registry.get("officecli_edit") is None

    asyncio.run(verify_runtime())
