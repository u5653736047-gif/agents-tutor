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
