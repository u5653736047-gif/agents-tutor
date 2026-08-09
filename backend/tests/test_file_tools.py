"""Public LangChain tool contract for workspace-scoped read operations."""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import BaseTool

from core.events import ErrorCode
from core.filesystem import WorkspaceFileSystem, workspace_scope
from core.state import AgentRole
from core.tools import ToolExecutor, ToolRegistry
from core.tools.file_tools import create_read_only_file_tools


def _tool_map(filesystem: WorkspaceFileSystem) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in create_read_only_file_tools(filesystem)}


def test_factory_exposes_only_the_expected_read_only_tools(tmp_path: Path) -> None:
    filesystem = WorkspaceFileSystem(tmp_path)

    tools = create_read_only_file_tools(filesystem)

    assert tuple(tool.name for tool in tools) == (
        "workspace_info",
        "list_files",
        "glob_files",
        "grep_files",
        "read_file",
        "inspect_workspace",
    )
    assert all(tool.extras == {"category": "filesystem", "read_only": True} for tool in tools)


def test_workspace_info_tool_describes_the_absolute_read_only_root(tmp_path: Path) -> None:
    workspace = tmp_path / "course-project"
    workspace.mkdir()
    tool = _tool_map(WorkspaceFileSystem(workspace))["workspace_info"]

    result = tool.invoke({})

    assert result == {
        "ok": True,
        "workspace_name": "course-project",
        "root": str(workspace.resolve()),
        "additional_roots": [],
        "access": "read_only",
        "path_format": "relative_or_absolute",
    }
    assert "工作区" in tool.description


def test_file_tools_follow_the_active_session_workspace_scope(tmp_path: Path) -> None:
    default = tmp_path / "default"
    selected = tmp_path / "selected"
    shared = tmp_path / "shared"
    default.mkdir()
    selected.mkdir()
    shared.mkdir()
    shared_file = shared / "notes.md"
    shared_file.write_text("session notes", encoding="utf-8")
    tools = _tool_map(WorkspaceFileSystem(default))

    with workspace_scope(selected, additional_roots=[shared]):
        scoped_info = tools["workspace_info"].invoke({})
        scoped_read = tools["read_file"].invoke({"path": str(shared_file)})

    default_info = tools["workspace_info"].invoke({})
    assert scoped_info["root"] == str(selected.resolve())
    assert scoped_info["additional_roots"] == [str(shared.resolve())]
    assert scoped_read["content"] == "1: session notes"
    assert default_info["root"] == str(default.resolve())


def test_read_file_tool_returns_structured_content(tmp_path: Path) -> None:
    (tmp_path / "lesson.txt").write_text("first\nsecond\n", encoding="utf-8")
    tools = _tool_map(WorkspaceFileSystem(tmp_path))

    result = tools["read_file"].invoke({"path": "lesson.txt", "line_offset": 2, "n_lines": 1})

    assert result["ok"] is True
    assert result["content"] == "2: second"
    assert result["path"] == "lesson.txt"


def test_file_tool_returns_safe_recoverable_policy_error(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("must-not-leak", encoding="utf-8")
    tools = _tool_map(WorkspaceFileSystem(tmp_path))

    result = tools["read_file"].invoke({"path": "../outside.txt"})

    assert result == {
        "ok": False,
        "error_code": "path_outside_workspace",
        "message": "只能访问当前会话已授权的工作区目录。",
    }
    assert "must-not-leak" not in str(result)
    assert str(tmp_path) not in str(result)


def test_tool_schema_rejects_unknown_and_out_of_range_arguments(tmp_path: Path) -> None:
    read_tool = _tool_map(WorkspaceFileSystem(tmp_path))["read_file"]
    registry = ToolRegistry()
    registry.register(read_tool, allowed_roles={AgentRole.SUPERVISOR})
    executor = ToolExecutor(registry)

    unknown_argument = executor.execute(
        {
            "id": "call-extra",
            "name": "read_file",
            "args": {"path": "lesson.txt", "unexpected": True},
        },
        AgentRole.SUPERVISOR,
    )
    out_of_range = executor.execute(
        {
            "id": "call-lines",
            "name": "read_file",
            "args": {"path": "lesson.txt", "n_lines": 1001},
        },
        AgentRole.SUPERVISOR,
    )

    assert unknown_argument.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    assert out_of_range.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS


def test_discovery_and_search_tools_forward_bounded_results(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lesson.md").write_text("Neural networks\n", encoding="utf-8")
    tools = _tool_map(WorkspaceFileSystem(tmp_path))

    listed = tools["list_files"].invoke({"path": "."})
    globbed = tools["glob_files"].invoke({"pattern": "**/*.md"})
    searched = tools["grep_files"].invoke({"query": "neural", "file_pattern": "**/*.md"})

    assert listed["entries"] == [{"path": "docs", "type": "directory"}]
    assert globbed["matches"] == ["docs/lesson.md"]
    assert searched["matches"] == [
        {"path": "docs/lesson.md", "line_number": 1, "line": "Neural networks"}
    ]


def test_inspect_workspace_runs_multiple_read_operations_in_one_tool_call(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    lesson = docs / "lesson.md"
    lesson.write_text("Neural networks\nBackpropagation\n", encoding="utf-8")
    tool = _tool_map(WorkspaceFileSystem(tmp_path))["inspect_workspace"]

    result = tool.invoke(
        {
            "operations": [
                {"operation": "list", "path": "."},
                {
                    "operation": "grep",
                    "query": "Backpropagation",
                    "file_pattern": "**/*.md",
                },
                {"operation": "read", "path": "docs/lesson.md", "n_lines": 1},
            ]
        }
    )

    assert result["ok"] is True
    assert result["operation_count"] == 3
    assert result["truncated"] is False
    assert [item["operation"] for item in result["results"]] == [
        "list",
        "grep",
        "read",
    ]
    assert result["results"][0]["result"]["entries"] == [
        {"path": "docs", "type": "directory"}
    ]
    assert result["results"][1]["result"]["matches"][0]["line_number"] == 2
    assert result["results"][2]["result"]["content"] == "1: Neural networks"
