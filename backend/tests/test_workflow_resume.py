"""工作流审批暂停/恢复与产物区豁免边界测试（lesson-workflow-design M5-1）。

真实冒烟发现的两个缺陷的回归锚：
1. 恢复后 Worker 簿记只认 RUNNING → PAUSED_APPROVAL 状态被跳过、步骤
   卡 RUNNING、调度节点防御性 raise、整轮图异常终止——本文件第一个
   用例覆盖「暂停→批准→恢复→完成收口」全链路；
2. 调度节点对 RUNNING 步骤的防御性 raise 已改为重入语义（重分派）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.events import EventType
from core.filesystem import WorkspaceFileSystem
from core.graph_builder import CollaborativeAgentGraph
from core.state import (
    AgentRole,
    ToolApprovalAction,
    ToolApprovalDecision,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepStatus,
)
from core.tools.office_tools import (
    OfficeCliSettings,
    create_office_tools,
)
from tests.test_graph_builder import ScriptedModel
from tests.test_workflow_orchestration import _start_call


def _text(text: str) -> AIMessage:
    return AIMessage(content=text)


def test_workflow_survives_approval_pause_and_resume(tmp_path: Path) -> None:
    """暂停→批准→恢复→四步完成收口：状态机全程一致（回归锚 1）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.xlsx").write_bytes(b"xlsx")
    filesystem = WorkspaceFileSystem(workspace)
    edit_tool = create_office_tools(
        filesystem,
        OfficeCliSettings(binary="officecli-missing-in-test"),
    )[1]

    model = ScriptedModel(
        [
            _start_call(),
            _text("正在启动教案工作流。"),
            # collect 步骤发起产物区外写操作 → 触发人工审批暂停
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "officecli_edit",
                        "args": {"command": ["set", "a.xlsx", "v"]},
                        "id": "edit-1",
                        "type": "tool_call",
                    }
                ],
            ),
            # 审批恢复后 TA 携批准结果重跑：给出 collect 终态回答
            _text("素材稿完成。"),
            # draft（再次分派 TA）
            _text("教案全文……"),
            # generate（TA 写文档）
            _text("已写入六段内容。"),
            # review（evaluator）通过
            _text('{"verdict": "pass", "summary": "结构完整"}'),
            # Supervisor 收口
            _text("教案已完成，请下载查看。"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        tools=[edit_tool],
        tool_permissions={"officecli_edit": {AgentRole.TEACHING_ASSISTANT}},
        checkpointer=InMemorySaver(),
        orchestration_mode="tool",
        enable_workflows=True,
    )

    paused = graph.run(
        "帮我准备反向传播的教案",
        session_id="wf-approve",
        workspace_root=str(workspace),
    )
    assert paused["pending_tool_approval"] is not None
    paused_workflow = WorkflowState.model_validate(paused["workflow"])
    assert paused_workflow.status is WorkflowStatus.PAUSED_APPROVAL
    assert paused_workflow.steps[0].status is WorkflowStepStatus.RUNNING
    # collect 步骤只分派过一次
    assert paused_workflow.steps[0].attempts == 1

    pending = graph.get_pending_tool_approval("wf-approve")
    assert pending is not None
    resumed = graph.resume_tool_approval(
        "wf-approve",
        ToolApprovalDecision(
            interrupt_id=pending.interrupt_id,
            action=ToolApprovalAction.CONFIRM,
        ),
    )

    final = WorkflowState.model_validate(resumed["workflow"])
    assert final.status is WorkflowStatus.COMPLETED
    assert [step.status for step in final.steps] == [
        WorkflowStepStatus.COMPLETED
    ] * 4
    # 恢复不重复计数：collect 步骤仍是首次分派的一次
    assert final.steps[0].attempts == 1
    event_types = [event.event_type for event in resumed["events"]]
    assert EventType.WORKFLOW_STEP_COMPLETED in event_types
    assert EventType.WORKFLOW_COMPLETED in event_types
    assert EventType.RUN_COMPLETED in event_types
    # 脚本恰好耗尽：无静默欠消耗、无意外多轮
    assert len(model.responses) == 0


def test_dispatch_redispatches_running_step_instead_of_raising() -> None:
    """调度节点对 RUNNING 步骤按重入语义重分派（回归锚 2）。"""
    graph = CollaborativeAgentGraph(
        model=ScriptedModel([]),
        orchestration_mode="tool",
        enable_workflows=True,
    )
    from core.workflows import get_workflow

    definition = get_workflow("lesson_plan")
    assert definition is not None
    workflow = definition.build_state({"topic": "t", "grade_hint": ""})
    workflow = workflow.model_copy(
        update={
            "steps": [
                workflow.steps[0].model_copy(
                    update={
                        "status": WorkflowStepStatus.COMPLETED,
                        "attempts": 1,
                    }
                ),
                workflow.steps[1].model_copy(
                    update={
                        "status": WorkflowStepStatus.COMPLETED,
                        "attempts": 1,
                    }
                ),
                workflow.steps[2].model_copy(
                    update={
                        "status": WorkflowStepStatus.RUNNING,
                        "attempts": 1,
                    }
                ),
                workflow.steps[3],
            ],
            "current_step_index": 2,
            "params": {"topic": "反向传播", "grade_hint": ""},
        }
    )
    updates = graph._workflow_dispatch(
        {
            "session_id": "s",
            "run_id": "r",
            "events": [],
            "workflow": workflow,
            "handoff_count": 0,
            "agent_switch_count": 0,
        }
    )
    # 不再 raise：重入分派同一 generate 步骤，attempts 递增
    assert updates["next_agent"] == "teaching_assistant"
    result = WorkflowState.model_validate(updates["workflow"])
    assert result.steps[2].status is WorkflowStepStatus.RUNNING
    assert result.steps[2].attempts == 2


def test_office_gate_exempts_empty_file_commands_in_zone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """空文件命令（如 help 探针）在产物区上下文免审批——模型自纠错
    而不惊动用户（真实冒烟：help 探针曾触发人工审批暂停）。"""
    from core.tools import office_tools
    from core.tools.artifact_scope import artifact_auto_approval

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    filesystem = WorkspaceFileSystem(workspace)
    _inspect, edit = create_office_tools(
        filesystem,
        OfficeCliSettings(binary=sys_executable()),
    )
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        office_tools,
        "_run_officecli",
        lambda *args, **kwargs: captured.append(args) or {"ok": False},
    )
    with artifact_auto_approval([str(workspace / "zone")]):
        result = edit.invoke({"command": ["help"]})
    # 不再 PermissionError：命令照常进入工具入口，被读动词白名单拒绝
    assert result["ok"] is False
    assert "officecli_inspect" in str(result["message"])
    # 帮助类命令不产生写副作用，runner 未被调用
    assert captured == []


def sys_executable() -> str:
    import sys

    return sys.executable


