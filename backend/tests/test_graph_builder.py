"""统一 ReAct Agent 的 LangGraph 编排测试。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from core.graph_builder import CollaborativeAgentGraph
from core.nodes.react_agent import ReActAgentNode
from core.state import AgentRole, create_initial_state


class ScriptedModel:
    """按图执行顺序返回预设模型消息。"""

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


def test_graph_routes_worker_back_to_supervisor() -> None:
    model = ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "handoff",
                        "args": {"target": "teaching_assistant"},
                        "id": "handoff-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="任务已分派"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    builder = CollaborativeAgentGraph(model=model)
    app = builder.build()
    state = create_initial_state(session_id="graph-test")
    state["messages"] = [HumanMessage(content="请解释梯度下降")]

    result = app.invoke(state)

    assert {type(agent) for agent in builder.agents.values()} == {ReActAgentNode}
    assert set(builder.agents) == set(AgentRole)
    assert "handoff" in model.bound_tool_names
    assert result["current_agent"] == "supervisor"
    assert result["next_agent"] is None
    assert result["messages"][-1].content == "最终汇总"
    assert len(result["tool_results"]) == 1
    assert len(model.calls) == 4
