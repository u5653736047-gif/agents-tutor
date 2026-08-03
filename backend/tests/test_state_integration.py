"""验证 AgentState 与 LangGraph StateGraph 的集成."""

import pytest
from langgraph.graph import END, StateGraph
from pydantic import ValidationError

from core.events import ErrorCode, EventType, RunError, RunEvent
from core.state import (
    AgentRole,
    AgentState,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    TaskPlan,
    TaskPlanStatus,
    TaskStepResult,
    create_initial_state,
)


def supervisor_node(state: AgentState) -> dict:
    """模拟 Supervisor 节点：设置当前 Agent 并结束."""
    return {"current_agent": "supervisor", "next_agent": None}


# 构建最小图
graph = StateGraph(AgentState)
graph.add_node("supervisor", supervisor_node)
graph.set_entry_point("supervisor")
graph.add_edge("supervisor", END)

app = graph.compile()

# 执行
result = app.invoke({"messages": [], "tool_results": []})
agent = result["current_agent"]
print(f"LangGraph StateGraph integration OK, current_agent={agent}")
assert agent == "supervisor"
print("All assertions passed!")


def test_initial_state_has_runtime_defaults() -> None:
    state = create_initial_state()

    assert state["events"] == []
    assert state["run_error"] is None
    assert state["handoff_count"] == 0
    assert state["agent_switch_count"] == 0
    assert state["pending_handoff"] is None
    assert state["task_plan"] is None
    assert state["task_results"] == []


def test_task_step_result_enforces_success_and_failure_contracts() -> None:
    success = TaskStepResult(
        step_sequence=1,
        target_agent=AgentRole.TEACHING_ASSISTANT,
        success=True,
        output="教学结果",
    )
    failure = TaskStepResult(
        step_sequence=2,
        target_agent=AgentRole.EVALUATOR,
        success=False,
        error_code=ErrorCode.MODEL_CALL_FAILED,
    )

    assert success.output == "教学结果"
    assert success.error_code is None
    assert failure.output is None
    assert failure.error_code is ErrorCode.MODEL_CALL_FAILED


@pytest.mark.parametrize(
    "values",
    [
        {
            "step_sequence": 1,
            "target_agent": AgentRole.TEACHING_ASSISTANT,
            "success": True,
            "output": "   ",
        },
        {
            "step_sequence": 1,
            "target_agent": AgentRole.TEACHING_ASSISTANT,
            "success": True,
            "output": "结果",
            "error_code": ErrorCode.MODEL_CALL_FAILED,
        },
        {
            "step_sequence": 1,
            "target_agent": AgentRole.TEACHING_ASSISTANT,
            "success": False,
        },
        {
            "step_sequence": 1,
            "target_agent": AgentRole.TEACHING_ASSISTANT,
            "success": False,
            "output": "不可靠的局部结果",
            "error_code": ErrorCode.MODEL_CALL_FAILED,
        },
        {
            "step_sequence": 1,
            "target_agent": AgentRole.SUPERVISOR,
            "success": True,
            "output": "非法目标",
        },
    ],
    ids=[
        "blank-success",
        "success-with-error",
        "failure-without-error",
        "failure-with-output",
        "supervisor-target",
    ],
)
def test_task_step_result_rejects_invalid_contracts(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TaskStepResult(**values)


def test_task_plan_normalizes_and_preserves_structured_steps() -> None:
    plan = TaskPlan(
        steps=[
            {
                "sequence": 2,
                "description": "检查讲解准确性",
                "target_agent": AgentRole.EVALUATOR,
            },
            {
                "sequence": 1,
                "description": "讲解梯度下降",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
        ]
    )

    assert [step.sequence for step in plan.steps] == [1, 2]
    assert [step.description for step in plan.steps] == [
        "讲解梯度下降",
        "检查讲解准确性",
    ]
    assert plan.current_step_index == 0
    assert plan.status is TaskPlanStatus.ACTIVE


@pytest.mark.parametrize(
    "steps",
    [
        [
            {
                "sequence": 1,
                "description": "只有一步",
                "target_agent": AgentRole.EVALUATOR,
            }
        ],
        [
            {
                "sequence": 1,
                "description": "   ",
                "target_agent": AgentRole.EVALUATOR,
            },
            {
                "sequence": 2,
                "description": "有效步骤",
                "target_agent": AgentRole.TEACHING_ASSISTANT,
            },
        ],
        [
            {
                "sequence": 1,
                "description": "非法目标",
                "target_agent": AgentRole.SUPERVISOR,
            },
            {
                "sequence": 2,
                "description": "有效步骤",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        [
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.EVALUATOR,
            },
            {
                "sequence": 1,
                "description": "重复序号",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        [
            {
                "sequence": 1,
                "description": "第一步",
                "target_agent": AgentRole.EVALUATOR,
            },
            {
                "sequence": 3,
                "description": "缺号",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
        [
            {
                "sequence": 0,
                "description": "非正序号",
                "target_agent": AgentRole.EVALUATOR,
            },
            {
                "sequence": 1,
                "description": "有效步骤",
                "target_agent": AgentRole.EVALUATOR,
            },
        ],
    ],
    ids=[
        "single-step",
        "blank-description",
        "supervisor-target",
        "duplicate-sequence",
        "missing-sequence",
        "non-positive-sequence",
    ],
)
def test_task_plan_rejects_invalid_steps(steps: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        TaskPlan(steps=steps)


@pytest.mark.parametrize(
    "values",
    [
        {"action": HandoffApprovalAction.MODIFY},
        {
            "action": HandoffApprovalAction.CONFIRM,
            "target_agent": AgentRole.EVALUATOR,
        },
        {
            "action": HandoffApprovalAction.MODIFY,
            "target_agent": AgentRole.SUPERVISOR,
        },
        {
            "action": HandoffApprovalAction.MODIFY,
            "task_content": "   ",
        },
    ],
    ids=[
        "modify-without-changes",
        "confirm-with-changes",
        "supervisor-target",
        "blank-task",
    ],
)
def test_handoff_approval_decision_rejects_invalid_combinations(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        HandoffApprovalDecision(interrupt_id="interrupt-1", **values)


def test_runtime_fields_use_expected_reducers() -> None:
    first = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
    )
    second = RunEvent(
        event_type=EventType.AGENT_COMPLETED,
        sequence=1,
        session_id="session-1",
    )
    initial_error = RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="old",
    )

    runtime_graph = StateGraph(AgentState)
    runtime_graph.add_node(
        "update",
        lambda state: {
            "events": [second],
            "run_error": None,
            "handoff_count": 2,
            "agent_switch_count": 3,
        },
    )
    runtime_graph.set_entry_point("update")
    runtime_graph.add_edge("update", END)
    runtime_app = runtime_graph.compile()
    state = create_initial_state(session_id="session-1")
    state.update(
        events=[first],
        run_error=initial_error,
        handoff_count=1,
        agent_switch_count=1,
    )

    result = runtime_app.invoke(state)

    assert result["events"] == [first, second]
    assert result["run_error"] is None
    assert result["handoff_count"] == 2
    assert result["agent_switch_count"] == 3
