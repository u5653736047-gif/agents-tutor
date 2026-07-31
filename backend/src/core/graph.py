"""StateGraph 组装与条件路由（任务 1.1.2）."""

from __future__ import annotations

from collections.abc import Hashable

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from core.nodes import (
    EvaluatorNode,
    LearningAssistantNode,
    SupervisorNode,
    TeachingAssistantNode,
)
from core.state import AgentRole, AgentState


def route_by_next_agent(state: AgentState) -> str:
    """条件边路由：根据 ``State.next_agent`` 选择下一节点，None 时结束."""
    return state.get("next_agent") or END


def build_graph() -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """构建多智能体 StateGraph.

    Supervisor 通过条件边按意图分派任务；子 Agent 执行完成后通过普通边
    回到 Supervisor 汇总，任务全部完成时路由到 END。
    """
    graph = StateGraph(AgentState)

    graph.add_node("supervisor", SupervisorNode())
    graph.add_node("teaching_assistant", TeachingAssistantNode())
    graph.add_node("learning_assistant", LearningAssistantNode())
    graph.add_node("evaluator", EvaluatorNode())

    graph.set_entry_point("supervisor")

    node_names = {role.value for role in AgentRole}
    path_map: dict[Hashable, str] = {}
    for name in node_names:
        path_map[name] = name
    path_map[END] = END
    graph.add_conditional_edges("supervisor", route_by_next_agent, path_map)

    for name in node_names - {AgentRole.SUPERVISOR.value}:
        graph.add_edge(name, "supervisor")

    return graph.compile()