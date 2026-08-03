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
    # S4-T2：learning_assistant / teaching_assistant 新增检索约定
    # （search_knowledge 先检索再作答/生成，见
    # test_worker_prompts_define_retrieval_contract），当前最长仍为
    # Supervisor（约 412 字符），learning_assistant 约 224，均低于 500。
    assert max(map(len, ROLE_PROMPTS.values())) <= 500
    assert model.bind_count == 1


def test_learning_assistant_dynamic_prompt_length_is_bounded() -> None:
    """S2-T2：动态水平提示词（静态角色卡 + 水平锚点 + 档位指导词）总长受控。

    ROLE_PROMPTS 长度上限只覆盖静态角色卡；动态水平段按 state["level"]
    每轮追加（见 prompts.learning_assistant_system_prompt），这里锁
    「叠加后」的总长——现状最长约 290 字符（basic 档：静态角色卡 224
    + 水平锚点与换行 16 + basic 指导词 50），上限取 340 留 50 字符
    余量，防止未来指导词膨胀撑爆上下文预算。
    """
    lengths = [
        len(learning_assistant_system_prompt(level))
        for level in (None, "basic", "advanced")
    ]
    assert max(lengths) <= 340


def test_worker_prompts_define_retrieval_contract() -> None:
    """S4-T2：答疑与备课角色提示词写明检索约定（先检索再作答/生成）。

    search_knowledge 已注入 learning_assistant 与 teaching_assistant
    （授权见 api/app.py），但工具在列表里不等于模型知道何时该用——
    线上冒烟实测答疑轮模型跳过检索、直接编造「已完成知识库检索」的
    幻觉回答。提示词必须写明：教材/知识性任务先调用工具再作答/生成，
    无命中时如实说明「知识库未覆盖」（与 S4-T3 阈值语义一致）。
    """
    learning = ROLE_PROMPTS[AgentRole.LEARNING_ASSISTANT]
    teaching = ROLE_PROMPTS[AgentRole.TEACHING_ASSISTANT]
    assert "search_knowledge" in learning  # 明确点名检索工具
    assert "检索" in learning
    assert "知识库未覆盖" in learning  # 无命中时如实说明，不强行作答
    assert "编造" in learning  # 禁止凭空编造教材内容
    assert "search_knowledge" in teaching
    assert "检索" in teaching
    assert "凭空编写" in teaching  # 备课禁止脱离教材凭空编写


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
