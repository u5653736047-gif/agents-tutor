"""角色 Prompt 与统一 Agent 工厂测试。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool

from core.nodes.factory import create_agent_nodes
from core.nodes.prompts import ROLE_PROMPTS
from core.nodes.react_agent import ReActAgentNode
from core.state import AgentRole


class BindableModel:
    """记录工具绑定次数的最小模型替身。"""

    def __init__(self) -> None:
        self.bind_count = 0

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content="unused")

    def bind_tools(self, tools: object) -> BindableModel:
        self.bind_count += 1
        return self


@tool
def double(value: int) -> int:
    """返回输入数字的两倍。"""
    return value * 2


def test_factory_builds_same_agent_with_short_role_prompts() -> None:
    model = BindableModel()

    agents = create_agent_nodes(model=model, tools=[double], max_iterations=3)

    assert set(agents) == set(AgentRole)
    assert {type(agent) for agent in agents.values()} == {ReActAgentNode}
    assert {id(agent.model) for agent in agents.values()} == {id(model)}
    assert len({id(agent.tool_executor) for agent in agents.values()}) == 1
    assert {agent.max_iterations for agent in agents.values()} == {3}
    assert {agent.system_prompt for agent in agents.values()} == set(ROLE_PROMPTS.values())
    assert max(map(len, ROLE_PROMPTS.values())) <= 80
    assert model.bind_count == 1
