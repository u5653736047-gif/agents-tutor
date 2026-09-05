"""工作流产物区自动授权测试（lesson-workflow-design §五 / M3）。

覆盖：
1. officecli_edit 运行时门：产物区内命令免人工审批可执行；产物区外
   （含部分越界 batch）仍硬拒绝；shell 式旁路不存在（无授权上下文时
   不因产物区意外放行目录外文件）；
2. ToolExecutor.artifact_auto_approval_root：命中/不命中/非 officecli
   工具（shell 永不豁免）；
3. Worker 簿记的产物登记：产物区内生成文件进 workflow.artifacts，
   产物区外生成文件不登记。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from core.filesystem import WorkspaceFileSystem, workspace_scope
from core.graph_builder import CollaborativeAgentGraph
from core.state import (
    AgentRole,
    GeneratedFile,
    ToolResult,
    WorkflowState,
    WorkflowStepStatus,
)
from core.tools import office_tools
from core.tools.artifact_scope import artifact_auto_approval
from core.tools.executor import ToolExecutor
from core.tools.office_tools import (
    OfficeCliSettings,
    create_office_tools,
    office_targets_within_roots,
)
from core.tools.registry import ToolRegistry
from tests.test_graph_builder import ScriptedModel
from tests.test_office_tools import _capture_runner


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def settings() -> OfficeCliSettings:
    return OfficeCliSettings(binary=sys.executable)


def _make_tools(workspace: Path, settings: OfficeCliSettings):
    filesystem = WorkspaceFileSystem(workspace)
    return create_office_tools(filesystem, settings)


def test_targets_within_roots_accepts_zone_and_rejects_outside(
    workspace: Path,
    settings: OfficeCliSettings,
) -> None:
    zone = workspace / ".workflow-artifacts" / "run-1"
    zone.mkdir(parents=True)
    (zone / "教案-反向传播.docx").write_bytes(b"docx")
    _inspect, _edit = _make_tools(workspace, settings)
    (workspace / "a.xlsx").write_bytes(b"xlsx")

    with workspace_scope(str(workspace)):
        assert office_targets_within_roots(
            ["create", ".workflow-artifacts/run-1/新教案.docx"],
            [str(zone)],
        )
        assert not office_targets_within_roots(
            ["set", "a.xlsx", "v"],
            [str(zone)],
        )
        assert not office_targets_within_roots(
            ["create", "../escape.docx"],
            [str(zone)],
        )
        assert not office_targets_within_roots(
            ["not-a-verb", "x.docx"],
            [str(zone)],
        )


def test_office_gate_allows_zone_without_manual_approval(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    settings: OfficeCliSettings,
) -> None:
    zone = workspace / ".workflow-artifacts" / "run-1"
    zone.mkdir(parents=True)
    _inspect, edit = _make_tools(workspace, settings)
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with artifact_auto_approval([str(zone)]):
        result = edit.invoke(
            {"command": ["create", ".workflow-artifacts/run-1/教案-反向传播.docx"]}
        )

    assert result["ok"] is True
    assert captured, "产物区内命令应已执行"


def test_office_gate_still_rejects_outside_zone(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    settings: OfficeCliSettings,
) -> None:
    zone = workspace / ".workflow-artifacts" / "run-1"
    zone.mkdir(parents=True)
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    _inspect, edit = _make_tools(workspace, settings)
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(office_tools, "_run_officecli", _capture_runner(captured))

    with artifact_auto_approval([str(zone)]), pytest.raises(PermissionError):
        edit.invoke({"command": ["set", "a.xlsx", "v"]})

    assert captured == []


def test_executor_exempts_only_officecli_edit_within_roots(
    workspace: Path,
    settings: OfficeCliSettings,
) -> None:
    zone = workspace / ".workflow-artifacts" / "run-1"
    zone.mkdir(parents=True)
    inspect_tool, edit_tool = _make_tools(workspace, settings)
    registry = ToolRegistry()
    registry.register(edit_tool, allowed_roles={AgentRole.TEACHING_ASSISTANT})
    registry.register(inspect_tool, allowed_roles={AgentRole.TEACHING_ASSISTANT})
    executor = ToolExecutor(registry)

    with workspace_scope(str(workspace)):
        in_zone = executor.artifact_auto_approval_root(
            {
                "name": "officecli_edit",
                "args": {
                    "command": [
                        "create",
                        ".workflow-artifacts/run-1/教案-反向传播.docx",
                    ]
                },
            },
            (str(zone),),
        )
        out_zone = executor.artifact_auto_approval_root(
            {"name": "officecli_edit", "args": {"command": ["create", "a.docx"]}},
            (str(zone),),
        )
    assert in_zone == str(zone)
    assert out_zone is None

    # 未注册 officecli_edit 的执行器（如仅 shell 的图）恒不豁免
    empty_executor = ToolExecutor()
    with workspace_scope(str(workspace)):
        assert (
            empty_executor.artifact_auto_approval_root(
                {
                    "name": "officecli_edit",
                    "args": {"command": ["create", "x.docx"]},
                },
                (str(zone),),
            )
            is None
        )


def test_worker_updates_register_zone_artifacts_only() -> None:
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        orchestration_mode="tool",
        enable_workflows=True,
    )
    from core.workflows import get_workflow

    definition = get_workflow("lesson_plan")
    assert definition is not None
    artifact_root = "D:\\ws\\.workflow-artifacts\\run-1"
    workflow = definition.build_state(
        {"topic": "反向传播", "grade_hint": ""},
        artifact_root=artifact_root,
    )
    workflow = workflow.model_copy(
        update={
            "steps": [
                workflow.steps[0].model_copy(
                    update={
                        "status": WorkflowStepStatus.RUNNING,
                        "attempts": 1,
                    }
                ),
                *workflow.steps[1:],
            ],
            "current_step_index": 0,
        }
    )
    agent = graph.agents[AgentRole.TEACHING_ASSISTANT]

    from core.nodes.react_agent import ReActResult

    in_zone = GeneratedFile(
        path=f"{artifact_root}\\教案-反向传播.docx",
        name="教案-反向传播.docx",
        size=10,
        mtime_ns=1,
    )
    out_zone = GeneratedFile(
        path="D:\\ws\\a.docx",
        name="a.docx",
        size=1,
        mtime_ns=1,
    )
    tool_result = ToolResult(
        tool_call_id="call-1",
        tool_name="officecli_edit",
        agent_role=AgentRole.TEACHING_ASSISTANT,
        success=True,
        output=json.dumps(
            {
                "ok": True,
                "generated_files": [
                    in_zone.model_dump(),
                    out_zone.model_dump(),
                ],
            }
        ),
        duration_ms=1.0,
    )
    result = ReActResult(
        updates={
            "messages": [AIMessage(content="已生成文档")],
            "tool_results": [tool_result],
        },
        messages=[AIMessage(content="已生成文档")],
    )
    events: list = []
    updates = graph._workflow_worker_updates(
        {"workflow": workflow},
        agent,
        result,
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    assert updates is not None
    updated = WorkflowState.model_validate(updates["workflow"])
    # 产物区内文件登记为相对 POSIX 路径；产物区外文件不登记
    assert updated.artifacts == ["教案-反向传播.docx"]


def test_workflow_state_artifacts_survive_roundtrip() -> None:
    """产物登记的相对路径约束与 checkpoint 往返（model_validate 等价）。"""
    payload: dict[str, Any] = {
        "workflow_id": "lesson_plan",
        "status": "completed",
        "steps": [
            {
                "step_id": "collect",
                "worker_role": "teaching_assistant",
                "status": "completed",
                "attempts": 1,
                "summary": "完成",
            }
        ],
        "current_step_index": 1,
        "artifacts": ["教案-反向传播.docx"],
    }
    state = WorkflowState.model_validate(payload)
    assert state.artifacts == ["教案-反向传播.docx"]
    with pytest.raises(ValidationError):
        WorkflowState.model_validate({**payload, "artifacts": ["../x.docx"]})
