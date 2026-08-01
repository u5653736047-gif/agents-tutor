"""基于统一 ReAct Agent 的 LangGraph 编排。"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import Literal, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .nodes import ReActAgentNode, create_agent_nodes
from .nodes.react_agent import ChatModel
from .state import AgentRole, AgentState, ToolResult, create_initial_state

WorkerRole = Literal["teaching_assistant", "learning_assistant", "evaluator"]
CompiledAgentGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]


# supervisor 分派任务使用的工具函数
@tool
def handoff(target: WorkerRole) -> str:
    """将当前任务交给指定的专业 Agent。"""
    return target


class CollaborativeAgentGraph:
    """注册四个同构 ReAct Agent，并负责它们之间的路由。"""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Sequence[BaseTool] = (),
        max_iterations: int = 5,
    ) -> None:
        all_tools = [handoff, *tools]
        # 创建4个同构的agent，均遵循react设计范式
        self.agents = create_agent_nodes(
            model=model,
            tools=all_tools,
            max_iterations=max_iterations,
        )

        # 图缓存，避免重复编译
        self._app: CompiledAgentGraph | None = None

    def build(self) -> CompiledAgentGraph:
        """构建一次并缓存可执行图。"""
        # 返回已有图：如果已经构建过，则直接返回
        if self._app is not None:
            return self._app

        graph = StateGraph(AgentState)
        # 路由表 = 路由返回值 ： 图节点 （映射）
        routes: dict[Hashable, str] = {
            AgentRole.SUPERVISOR.value: AgentRole.SUPERVISOR.value,
            AgentRole.TEACHING_ASSISTANT.value: AgentRole.TEACHING_ASSISTANT.value,
            AgentRole.LEARNING_ASSISTANT.value: AgentRole.LEARNING_ASSISTANT.value,
            AgentRole.EVALUATOR.value: AgentRole.EVALUATOR.value,
            "end": END,
        }

        # 注册所有agent
        for role, agent in self.agents.items():
            graph.add_node(role.value, self._wrap(agent))
            graph.add_conditional_edges(role.value, self._route, routes)

        # 设置入口节点并编译图
        graph.set_entry_point(AgentRole.SUPERVISOR.value)
        self._app = graph.compile()
        return self._app

    @staticmethod
    def _wrap(agent: ReActAgentNode) -> Runnable[AgentState, AgentState]:
        """把 ReAct 结果转换为 LangGraph 状态更新。"""

        def node(state: AgentState) -> AgentState:
            # 运行 Agent
            result = agent.run(state)
            if result.error:
                raise RuntimeError(result.error)

            # 解读 Agent 的返回值
            updates = dict(result.updates)
            tool_results = cast(list[ToolResult], updates.get("tool_results", []))
            updates["next_agent"] = _handoff_target(tool_results)
            return cast(AgentState, updates)

        return RunnableLambda(node)

    @staticmethod
    def _route(state: AgentState) -> str:
        """有 handoff 时转给目标；Worker 完成后回到 Supervisor。"""
        next_agent = state.get("next_agent")
        if next_agent in {
            AgentRole.TEACHING_ASSISTANT.value,
            AgentRole.LEARNING_ASSISTANT.value,
            AgentRole.EVALUATOR.value,
        }:
            return next_agent
        if state.get("current_agent") != AgentRole.SUPERVISOR.value:
            return AgentRole.SUPERVISOR.value
        return "end"

    def run(self, user_input: str, session_id: str = "demo") -> AgentState:
        """从一条用户消息启动协作图。"""
        state = create_initial_state(session_id=session_id)
        state["messages"] = [HumanMessage(content=user_input)]
        return cast(AgentState, self.build().invoke(state))

    def get_node_info(self) -> dict[str, dict[str, str]]:
        """返回节点身份与 Prompt，便于调试和展示。"""
        return {
            role.value: {
                "role": role.value,
                "prompt": agent.system_prompt,
            }
            for role, agent in self.agents.items()
        }


def _handoff_target(tool_results: Sequence[ToolResult]) -> str | None:
    """只读取本次 Agent 调用产生的 handoff 结果。"""
    for result in reversed(tool_results):
        if result.tool_name == "handoff" and result.success:
            return result.output
    return None


__all__ = ["CollaborativeAgentGraph", "handoff"]
