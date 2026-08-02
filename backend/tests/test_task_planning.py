"""Supervisor 显式任务分解与确定性顺序调度测试。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from core.events import ErrorCode, EventType
from core.graph_builder import CollaborativeAgentGraph
from core.persistence import open_sqlite_checkpointer
from core.state import (
    AgentRole,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    TaskPlan,
    TaskPlanStatus,
)


class ScriptedModel:
    """按图执行顺序返回预设消息，并记录各角色可见上下文。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tool_names: list[str] = []

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        self.bound_tool_names = [str(getattr(tool, "name", "")) for tool in tools]
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def _plan_response() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_task_plan",
                "args": {
                    "steps": [
                        {
                            "sequence": 1,
                            "description": "讲解梯度下降",
                            "target_agent": "teaching_assistant",
                        },
                        {
                            "sequence": 2,
                            "description": "检查讲解准确性",
                            "target_agent": "evaluator",
                        },
                    ]
                },
                "id": "task-plan-1",
                "type": "tool_call",
            }
        ],
    )


def _handoff_response(target: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": "redundant-handoff",
                "type": "tool_call",
            }
        ],
    )


def _complex_responses() -> list[AIMessage]:
    return [
        _plan_response(),
        AIMessage(content="计划已创建"),
        AIMessage(content="教学结果"),
        AIMessage(content="评价结果"),
        AIMessage(content="最终汇总"),
    ]


def _role_name(messages: Sequence[BaseMessage]) -> str:
    prompt = str(messages[0].content)
    if "协调者" in prompt:
        return AgentRole.SUPERVISOR.value
    if "助教" in prompt:
        return AgentRole.TEACHING_ASSISTANT.value
    if "评价助手" in prompt:
        return AgentRole.EVALUATOR.value
    return AgentRole.LEARNING_ASSISTANT.value


def _latest_human(messages: Sequence[BaseMessage]) -> str:
    return next(
        str(message.content)
        for message in reversed(messages)
        if isinstance(message, HumanMessage)
    )


def test_complex_request_creates_plan_and_dispatches_in_order() -> None:
    model = ScriptedModel(_complex_responses())
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请先讲解梯度下降，再检查讲解是否准确", "complex-plan")
    plan = TaskPlan.model_validate(result["task_plan"])

    assert "create_task_plan" in model.bound_tool_names
    assert [_role_name(call) for call in model.calls] == [
        AgentRole.SUPERVISOR.value,
        AgentRole.SUPERVISOR.value,
        AgentRole.TEACHING_ASSISTANT.value,
        AgentRole.EVALUATOR.value,
        AgentRole.SUPERVISOR.value,
    ]
    assert _latest_human(model.calls[2]) == "讲解梯度下降"
    assert _latest_human(model.calls[3]) == "检查讲解准确性"
    assert [step.sequence for step in plan.steps] == [1, 2]
    assert plan.current_step_index == 2
    assert plan.status is TaskPlanStatus.COMPLETED
    assert result["handoff_count"] == 2
    assert result["agent_switch_count"] == 4
    assert [
        event.agent
        for event in result["events"]
        if event.event_type is EventType.AGENT_SWITCHED
    ] == ["teaching_assistant", "supervisor", "evaluator", "supervisor"]
    assert [item.tool_name for item in result["tool_results"]] == [
        "create_task_plan"
    ]
    assert result["messages"][-1].content == "最终汇总"


def test_task_plan_tool_schema_only_advertises_worker_targets() -> None:
    graph = CollaborativeAgentGraph(model=ScriptedModel([]))
    planning_tool = graph.registry.get("create_task_plan")

    assert planning_tool is not None
    assert planning_tool.args_schema is not None
    schema_text = json.dumps(planning_tool.args_schema.model_json_schema())
    assert "supervisor" not in schema_text
    assert all(
        role.value in schema_text
        for role in (
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.LEARNING_ASSISTANT,
            AgentRole.EVALUATOR,
        )
    )


def test_simple_request_directly_handoffs_without_plan_or_extra_round() -> None:
    model = ScriptedModel(
        [
            _handoff_response("teaching_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请解释梯度下降", "simple-dispatch")

    assert len(model.calls) == 4
    assert result["task_plan"] is None
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2
    assert all(
        item.tool_name != "create_task_plan" for item in result["tool_results"]
    )
    assert _latest_human(model.calls[2]) == "请解释梯度下降"


def test_plan_rejects_mismatched_redundant_handoff_before_worker_runs() -> None:
    model = ScriptedModel(
        [
            _plan_response(),
            _handoff_response("evaluator"),
            AIMessage(content="不得执行"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("复杂任务", "mismatched-plan")
    plan = TaskPlan.model_validate(result["task_plan"])

    assert [_role_name(call) for call in model.calls] == [
        AgentRole.SUPERVISOR.value,
        AgentRole.SUPERVISOR.value,
        AgentRole.SUPERVISOR.value,
    ]
    assert plan.current_step_index == 0
    assert plan.status is TaskPlanStatus.FAILED
    assert result["handoff_count"] == 0
    assert result["agent_switch_count"] == 0
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_INVALID_TARGET


@pytest.mark.parametrize(
    "final_tool_call",
    [_handoff_response("learning_assistant"), _plan_response()],
    ids=["extra-handoff", "replacement-plan"],
)
def test_completed_plan_rejects_additional_supervisor_dispatch(
    final_tool_call: AIMessage,
) -> None:
    model = ScriptedModel(
        [
            _plan_response(),
            AIMessage(content="计划已创建"),
            AIMessage(content="教学结果"),
            AIMessage(content="评价结果"),
            final_tool_call,
            AIMessage(content="不得继续分派"),
            AIMessage(content="计划外 Worker 不得执行"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("复杂任务", "completed-plan-guard")
    plan = TaskPlan.model_validate(result["task_plan"])

    assert len(model.calls) == 6
    assert all(
        _role_name(call) == AgentRole.SUPERVISOR.value
        for call in model.calls[4:]
    )
    assert plan.current_step_index == 2
    assert plan.status is TaskPlanStatus.FAILED
    assert result["handoff_count"] == 2
    assert result["agent_switch_count"] == 4
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_INVALID_TARGET


@pytest.mark.parametrize(
    ("options", "expected_error"),
    [
        ({"max_handoffs": 1}, ErrorCode.GRAPH_HANDOFF_LIMIT),
        ({"max_agent_switches": 2}, ErrorCode.GRAPH_SWITCH_LIMIT),
    ],
)
def test_planned_dispatches_still_obey_runtime_limits(
    options: dict[str, int],
    expected_error: ErrorCode,
) -> None:
    model = ScriptedModel(_complex_responses())
    graph = CollaborativeAgentGraph(model=model, **options)

    result = graph.run("复杂任务", f"plan-limit-{expected_error.value}")
    plan = TaskPlan.model_validate(result["task_plan"])

    assert len(model.calls) == 3
    assert _role_name(model.calls[-1]) == AgentRole.TEACHING_ASSISTANT.value
    assert plan.current_step_index == 1
    assert plan.status is TaskPlanStatus.FAILED
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2
    assert result["run_error"] is not None
    assert result["run_error"].error_code is expected_error
    assert result["events"][-1].event_type is EventType.RUN_FAILED


def test_final_worker_switch_limit_marks_plan_failed() -> None:
    model = ScriptedModel(_complex_responses())
    graph = CollaborativeAgentGraph(model=model, max_agent_switches=3)

    result = graph.run("复杂任务", "final-worker-switch-limit")
    plan = TaskPlan.model_validate(result["task_plan"])

    assert len(model.calls) == 4
    assert plan.current_step_index == 2
    assert plan.status is TaskPlanStatus.FAILED
    assert result["handoff_count"] == 2
    assert result["agent_switch_count"] == 3
    assert result["run_error"] is not None
    assert result["run_error"].error_code is ErrorCode.GRAPH_SWITCH_LIMIT


def test_planned_dispatches_reuse_handoff_approval_gate() -> None:
    model = ScriptedModel(_complex_responses())
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "planned-approval"

    first_pause = graph.run("复杂任务", session_id, "user-1")
    first_pending = graph.get_pending_handoff(session_id, "user-1")
    first_plan = TaskPlan.model_validate(first_pause["task_plan"])

    assert first_pending is not None
    assert first_pending.request.target_agent is AgentRole.TEACHING_ASSISTANT
    assert first_pending.request.task_content == "讲解梯度下降"
    assert first_pending.request.plan_step_sequence == 1
    assert first_plan.current_step_index == 0
    assert first_pause["handoff_count"] == 0

    second_pause = graph.resume_handoff(
        session_id,
        HandoffApprovalDecision(
            interrupt_id=first_pending.interrupt_id,
            action=HandoffApprovalAction.CONFIRM,
        ),
        "user-1",
    )
    second_pending = graph.get_pending_handoff(session_id, "user-1")
    second_plan = TaskPlan.model_validate(second_pause["task_plan"])

    assert second_pending is not None
    assert second_pending.request.target_agent is AgentRole.EVALUATOR
    assert second_pending.request.task_content == "检查讲解准确性"
    assert second_pending.request.plan_step_sequence == 2
    assert second_plan.current_step_index == 1
    assert second_pause["handoff_count"] == 1

    result = graph.resume_handoff(
        session_id,
        HandoffApprovalDecision(
            interrupt_id=second_pending.interrupt_id,
            action=HandoffApprovalAction.CONFIRM,
        ),
        "user-1",
    )
    completed_plan = TaskPlan.model_validate(result["task_plan"])

    assert completed_plan.current_step_index == 2
    assert completed_plan.status is TaskPlanStatus.COMPLETED
    assert result["handoff_count"] == 2
    assert result["agent_switch_count"] == 4
    assert graph.get_pending_handoff(session_id, "user-1") is None


def test_rejected_planned_handoff_cancels_without_redispatch() -> None:
    model = ScriptedModel(_complex_responses())
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "rejected-plan"
    graph.run("复杂任务", session_id, "user-1")
    pending = graph.get_pending_handoff(session_id, "user-1")
    assert pending is not None

    result = graph.resume_handoff(
        session_id,
        HandoffApprovalDecision(
            interrupt_id=pending.interrupt_id,
            action=HandoffApprovalAction.REJECT,
        ),
        "user-1",
    )
    plan = TaskPlan.model_validate(result["task_plan"])

    assert len(model.calls) == 2
    assert plan.current_step_index == 0
    assert plan.status is TaskPlanStatus.CANCELLED
    assert result["handoff_count"] == 0
    assert result["agent_switch_count"] == 0


def test_modified_planned_handoff_updates_audited_step_and_worker_task() -> None:
    model = ScriptedModel(_complex_responses())
    graph = CollaborativeAgentGraph(
        model=model,
        checkpointer=InMemorySaver(),
        interrupt_before_handoff=True,
    )
    session_id = "modified-plan"
    graph.run("复杂任务", session_id, "user-1")
    pending = graph.get_pending_handoff(session_id, "user-1")
    assert pending is not None

    paused_again = graph.resume_handoff(
        session_id,
        HandoffApprovalDecision(
            interrupt_id=pending.interrupt_id,
            action=HandoffApprovalAction.MODIFY,
            target_agent=AgentRole.LEARNING_ASSISTANT,
            task_content="用例子讲解梯度下降",
        ),
        "user-1",
    )
    plan = TaskPlan.model_validate(paused_again["task_plan"])

    assert _role_name(model.calls[2]) == AgentRole.LEARNING_ASSISTANT.value
    assert _latest_human(model.calls[2]) == "用例子讲解梯度下降"
    assert plan.steps[0].target_agent is AgentRole.LEARNING_ASSISTANT
    assert plan.steps[0].description == "用例子讲解梯度下降"
    assert plan.current_step_index == 1
    assert graph.get_pending_handoff(session_id, "user-1") is not None


def test_new_user_turn_clears_completed_plan_before_simple_dispatch() -> None:
    model = ScriptedModel(
        [
            *_complex_responses(),
            _handoff_response("teaching_assistant"),
            AIMessage(content="任务已分派"),
            AIMessage(content="新的教学结果"),
            AIMessage(content="新的最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model, checkpointer=InMemorySaver())
    graph.run("复杂任务", "next-turn-plan", "user-1")

    result = graph.run("简单任务", "next-turn-plan", "user-1")

    assert len(model.calls) == 9
    assert result["task_plan"] is None
    assert result["task_results"] == []
    assert result["handoff_count"] == 1
    assert result["agent_switch_count"] == 2


def test_task_plan_survives_sqlite_graph_reopen(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "plans" / "checkpoints.sqlite"
    first_model = ScriptedModel(_complex_responses())

    with open_sqlite_checkpointer(checkpoint_path) as first_saver:
        first_graph = CollaborativeAgentGraph(
            model=first_model,
            checkpointer=first_saver,
        )
        first_graph.run("复杂任务", "persisted-plan", "user-1")

    second_model = ScriptedModel([])
    with open_sqlite_checkpointer(checkpoint_path) as second_saver:
        second_graph = CollaborativeAgentGraph(
            model=second_model,
            checkpointer=second_saver,
        )
        restored = second_graph.get_state("persisted-plan", "user-1")

    assert restored is not None
    plan = TaskPlan.model_validate(restored["task_plan"])
    assert [(step.sequence, step.target_agent) for step in plan.steps] == [
        (1, AgentRole.TEACHING_ASSISTANT),
        (2, AgentRole.EVALUATOR),
    ]
    assert plan.current_step_index == 2
    assert plan.status is TaskPlanStatus.COMPLETED
    assert restored["handoff_count"] == 2
    assert restored["agent_switch_count"] == 4
    assert second_model.calls == []
