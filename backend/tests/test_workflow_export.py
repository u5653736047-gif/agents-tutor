"""确定性导出链路测试（step_outputs 暂存 + export_workflow_docx）。

背景：真实冒烟两次产出空 docx——模型经 CLI 参数搬运整篇正文是三重脆
弱设计（语法发现耗迭代 / MAX_COMMAND_TOKENS / 预算耗尽后谎报完成）。
修复后正文走 WorkflowState.step_outputs 暂存 → 工具内部以代码构造的
create + add --type markdown --prop src= 命令写入 → view stats 自验。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, ConfigDict

from core.graph_builder import _ACTIVE_PARENT_STATE, CollaborativeAgentGraph
from core.state import (
    AgentRole,
    create_initial_state,
)
from core.workflows import get_workflow
from tests.test_graph_builder import ScriptedModel


def _graph() -> CollaborativeAgentGraph:
    return CollaborativeAgentGraph(
        model=ScriptedModel([]),
        orchestration_mode="tool",
        enable_workflows=True,
    )


def _staged_workflow(tmp_path: Path, draft: str) -> Any:
    definition = get_workflow("lesson_plan")
    assert definition is not None
    workflow = definition.build_state(
        {"topic": "反向传播", "grade_hint": ""},
        artifact_root=str(tmp_path / "zone"),
    )
    (tmp_path / "zone").mkdir(parents=True, exist_ok=True)
    return workflow.model_copy(
        update={
            "step_outputs": {"draft": draft},
            "current_step_index": 2,
            "steps": [
                workflow.steps[0].model_copy(
                    update={
                        "status": __import__(
                            "core.state", fromlist=["WorkflowStepStatus"]
                        ).WorkflowStepStatus.COMPLETED,
                        "attempts": 1,
                    }
                ),
                workflow.steps[1].model_copy(
                    update={
                        "status": __import__(
                            "core.state", fromlist=["WorkflowStepStatus"]
                        ).WorkflowStepStatus.COMPLETED,
                        "attempts": 1,
                    }
                ),
                workflow.steps[2],
                workflow.steps[3],
            ],
        }
    )


def _fake_office_tool(name: str, responses: list[dict[str, Any]]):
    captured: list[dict[str, Any]] = []

    class _Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

        command: list[str]

    @langchain_tool(name, args_schema=_Args)
    def fake(command: list[str]) -> dict[str, Any]:
        """Fake office tool capturing issued commands."""
        captured.append({"command": command})
        return responses[len(captured) - 1]

    return fake, captured


def test_worker_updates_stage_step_output(tmp_path: Path) -> None:
    """步骤成功后终端输出按 step_id 暂存进 step_outputs。"""
    graph = _graph()
    definition = get_workflow("lesson_plan")
    assert definition is not None
    workflow = definition.build_state({"topic": "t", "grade_hint": ""})
    from core.nodes.react_agent import ReActResult

    events: list = []
    updates = graph._workflow_worker_updates(
        {"workflow": workflow},
        graph.agents[AgentRole.TEACHING_ASSISTANT],
        ReActResult(
            updates={"messages": [], "tool_results": []},
            messages=[__import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(content="教案全文MD")],
        ),
        lambda *args, **kwargs: events.append(kwargs),
    )
    assert updates is not None
    staged = updates["workflow"].step_outputs
    assert staged.get("collect") == "教案全文MD"


def test_export_tool_writes_md_and_builds_commands(tmp_path: Path) -> None:
    """导出工具：暂存文本落 draft.md，命令 = create + add(src=)，自验通过。"""
    graph = _graph()
    definition = get_workflow("lesson_plan")
    assert definition is not None
    workflow = _staged_workflow(
        tmp_path,
        "## 一、教学目标\n\n理解链式法则。\n\n## 二、重难点\n\n链式法则的展开。",
    )
    parent = create_initial_state(
        session_id="s",
        user_id="u",
        run_id="run-1",
        workspace_root=str(tmp_path),
    )
    parent["workflow"] = workflow
    token = _ACTIVE_PARENT_STATE.set(parent)
    fake_edit, edit_calls = _fake_office_tool(
        "officecli_edit",
        [
            {"ok": True, "message": "created"},
            {"ok": True, "message": "added"},
        ],
    )
    fake_inspect, _inspect_calls = _fake_office_tool(
        "officecli_inspect",
        [{"ok": True, "stdout": "Paragraphs: 12 | Words: 300"}],
    )
    export_tool = graph._create_export_workflow_docx_tool(
        fake_edit,
        fake_inspect,
    )
    try:
        output = export_tool.invoke({})
    finally:
        _ACTIVE_PARENT_STATE.reset(token)

    receipt = json.loads(output)
    assert receipt["ok"] is True
    assert receipt["paragraphs"] == 12
    # 暂存文本落盘 draft.md（正文不经模型/CLI 参数）
    md = tmp_path / "zone" / "draft.md"
    assert "链式法则" in md.read_text(encoding="utf-8")
    # 命令形态：create + add --type markdown --prop src=
    assert edit_calls[0]["command"][0] == "create"
    assert edit_calls[1]["command"][:4] == ["add", str(tmp_path / "zone" / "教案-反向传播.docx"), "/body", "--type"]
    assert any(
        token.startswith("src=") for token in edit_calls[1]["command"]
    )


def test_export_tool_fails_closed_on_empty_document(tmp_path: Path) -> None:
    """写入自验：段落数为 0 时返回 ok=false（杜绝谎报完成）。"""
    graph = _graph()
    workflow = _staged_workflow(tmp_path, "## 一、教学目标\n\n内容。")
    parent = create_initial_state(
        session_id="s",
        user_id="u",
        run_id="run-1",
        workspace_root=str(tmp_path),
    )
    parent["workflow"] = workflow
    token = _ACTIVE_PARENT_STATE.set(parent)
    fake_edit, _calls = _fake_office_tool(
        "officecli_edit",
        [
            {"ok": True, "message": "created"},
            {"ok": True, "message": "added"},
        ],
    )
    fake_inspect, _i = _fake_office_tool(
        "officecli_inspect",
        [{"ok": True, "stdout": "Paragraphs: 0 | Words: 0"}],
    )
    export_tool = graph._create_export_workflow_docx_tool(
        fake_edit,
        fake_inspect,
    )
    try:
        output = export_tool.invoke({})
    finally:
        _ACTIVE_PARENT_STATE.reset(token)
    receipt = json.loads(output)
    assert receipt["ok"] is False
    assert "段落数为 0" in receipt["error"]


def test_export_tool_requires_staged_draft(tmp_path: Path) -> None:
    graph = _graph()
    definition = get_workflow("lesson_plan")
    assert definition is not None
    workflow = definition.build_state(
        {"topic": "t", "grade_hint": ""},
        artifact_root=str(tmp_path / "zone"),
    )
    (tmp_path / "zone").mkdir(parents=True, exist_ok=True)
    parent = create_initial_state(
        session_id="s",
        user_id="u",
        run_id="run-1",
        workspace_root=str(tmp_path),
    )
    parent["workflow"] = workflow
    token = _ACTIVE_PARENT_STATE.set(parent)
    fake_edit, _c = _fake_office_tool("officecli_edit", [])
    fake_inspect, _i = _fake_office_tool("officecli_inspect", [])
    export_tool = graph._create_export_workflow_docx_tool(
        fake_edit,
        fake_inspect,
    )
    try:
        output = export_tool.invoke({})
    finally:
        _ACTIVE_PARENT_STATE.reset(token)
    receipt = json.loads(output)
    assert receipt["ok"] is False
    assert "暂存区" in receipt["error"]
