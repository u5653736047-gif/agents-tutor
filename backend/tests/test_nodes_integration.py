"""验证 Agent 节点抽象与条件路由（任务 1.1.2）."""

from typing import Any

import pytest
from langgraph.graph import END

from core.graph import build_graph
from core.nodes import BaseAgentNode
from core.state import AgentRole, AgentState, TaskContext, create_initial_state


def _state_with_intent(intent: str, *, completed: bool = False) -> AgentState:
    """构造带指定意图任务上下文的初始状态."""
    state = create_initial_state(session_id="s1", user_id="u1")
    state["task_context"] = TaskContext(
        intent=intent,
        description="测试任务",
        subtasks=["子任务1"],
    )
    if completed:
        state["extra"] = {"task_completed": True}
    return state


class _SpyNode(BaseAgentNode):
    """按顺序记录四阶段调用，用于验证循环编排."""

    def __init__(self) -> None:
        super().__init__(AgentRole.SUPERVISOR)
        self.calls: list[str] = []

    def think(self, state: AgentState) -> Any:
        self.calls.append("think")
        return None

    def decide(self, state: AgentState, plan: Any) -> str:
        self.calls.append("decide")
        return "reply"

    def execute(self, state: AgentState, action: str) -> Any:
        self.calls.append("execute")
        return None

    def observe(self, state: AgentState, action: str, result: Any) -> dict[str, Any]:
        self.calls.append("observe")
        return {}


def test_base_node_runs_think_decide_execute_observe_in_order() -> None:
    """节点一次调用必须按 思考→决策→执行→观察 的顺序执行."""
    node = _SpyNode()

    node(_state_with_intent("learn"))

    assert node.calls == ["think", "decide", "execute", "observe"]


@pytest.mark.parametrize(
    ("intent", "worker"),
    [
        ("teach", "teaching_assistant"),
        ("learn", "learning_assistant"),
        ("evaluate", "evaluator"),
    ],
)
def test_graph_routes_intent_to_worker_and_terminates(intent: str, worker: str) -> None:
    """Supervisor 按意图分派到对应子 Agent，子 Agent 完成后回到 Supervisor 并结束."""
    result = build_graph().invoke(_state_with_intent(intent))

    names = [message.name for message in result["messages"]]
    assert worker in names
    assert "supervisor" in names
    assert result["current_agent"] == "supervisor"
    assert result["next_agent"] == END
    assert result["extra"]["task_completed"] is True


def test_graph_falls_back_to_teaching_assistant_for_unknown_intent() -> None:
    """未知意图默认交给助教 Agent 兜底."""
    result = build_graph().invoke(_state_with_intent("unknown"))

    names = [message.name for message in result["messages"]]
    assert "teaching_assistant" in names


def test_graph_ends_when_task_already_completed() -> None:
    """任务已完成时 Supervisor 直接结束，不再次分派."""
    result = build_graph().invoke(_state_with_intent("learn", completed=True))

    names = [message.name for message in result["messages"]]
    assert names == ["supervisor"]
    assert result["next_agent"] == END