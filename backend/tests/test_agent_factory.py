"""角色 Prompt 与统一 Agent 工厂测试。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool

from core.nodes.factory import create_agent_nodes
from core.nodes.prompts import ROLE_PROMPTS, learning_assistant_system_prompt
from core.nodes.react_agent import ReActAgentNode
from core.state import AgentRole
from core.tools import ToolRegistry


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


def count_context_messages(messages: Sequence[BaseMessage]) -> int:
    return len(messages)


def test_factory_builds_same_agent_with_short_role_prompts() -> None:
    model = BindableModel()

    agents = create_agent_nodes(
        model=model,
        tools=[double],
        max_iterations=3,
        max_context_messages=7,
        max_context_tokens=100,
        context_token_counter=count_context_messages,
    )

    assert set(agents) == set(AgentRole)
    assert {type(agent) for agent in agents.values()} == {ReActAgentNode}
    assert {id(agent.model) for agent in agents.values()} == {id(model)}
    assert len({id(agent.tool_executor) for agent in agents.values()}) == 1
    assert {agent.max_iterations for agent in agents.values()} == {3}
    assert {agent.max_context_messages for agent in agents.values()} == {7}
    assert {agent.max_context_tokens for agent in agents.values()} == {100}
    assert {
        agent.context_token_counter for agent in agents.values()
    } == {count_context_messages}
    for role, agent in agents.items():
        assert agent.system_prompt == ROLE_PROMPTS[role]
    # S2-T1：Supervisor 提示词新增了意图识别约定（detect_intent 与五类意图
    # 的路由说明），长度上限从 160 放宽到 400，其余角色提示词远低于此。
    # S2-T2：Supervisor 提示词又新增了学生水平识别约定（detect_level 的
    # 调用时机），上限再放宽到 500；其余角色提示词仍远低于此。
    assert max(map(len, ROLE_PROMPTS.values())) <= 500
    assert model.bind_count == 1


def test_learning_assistant_dynamic_prompt_length_is_bounded() -> None:
    """S2-T2：动态水平提示词（静态角色卡 + 水平锚点 + 档位指导词）总长受控。

    ROLE_PROMPTS 长度上限只覆盖静态角色卡；动态水平段按 state["level"]
    每轮追加（见 prompts.learning_assistant_system_prompt），这里锁
    「叠加后」的总长——现状最长约 176 字符（unknown 档），上限取 220
    留 44 字符余量，防止未来指导词膨胀撑爆上下文预算。
    """
    lengths = [
        len(learning_assistant_system_prompt(level))
        for level in (None, "basic", "advanced")
    ]
    assert max(lengths) <= 220


def test_factory_accepts_and_shares_registry() -> None:
    model = BindableModel()
    registry = ToolRegistry([double])

    agents = create_agent_nodes(model=model, registry=registry)

    assert model.bind_count == 1
    assert {id(agent.model) for agent in agents.values()} == {id(model)}
    assert len({id(agent.tool_executor) for agent in agents.values()}) == 1
    assert {
        id(agent.tool_executor.registry) for agent in agents.values()
    } == {id(registry)}
    assert {agent.max_context_messages for agent in agents.values()} == {None}
