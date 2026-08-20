"""角色 Prompt 与统一 Agent 工厂测试。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool

from core.nodes.factory import create_agent_nodes
from core.nodes.prompts import (
    ROLE_PROMPTS,
    TOOL_ORCHESTRATION_SUPERVISOR_PROMPT,
    learning_assistant_system_prompt,
)
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
    # test_worker_prompts_define_retrieval_contract）。文件工作区契约还会为
    # 三个有只读权限的角色追加约 120 字符；最长角色卡仍限制在 600 内，
    # 防止后续规则无边界膨胀。
    # officecli 集成（docs/officecli-integration-plan.md T3-2）：各角色追加
    # office 工具使用短策略（supervisor/助教/评价约 120 字符，助学约 60），
    # 最长角色卡现状 766，上限放宽到 800，仍防止无边界膨胀。
    # 六大功能 P2-11/P3-P5：supervisor 卡追加三个新意图路由说明、
    # evaluator 卡追加批改与学情诊断约定，现状最长 883（supervisor），
    # 上限放宽到 920，仍防止无边界膨胀。
    assert max(map(len, ROLE_PROMPTS.values())) <= 920
    assert model.bind_count == 1


def test_learning_assistant_dynamic_prompt_length_is_bounded() -> None:
    """S2-T2：动态水平提示词（静态角色卡 + 水平锚点 + 档位指导词）总长受控。

    ROLE_PROMPTS 长度上限只覆盖静态角色卡；动态水平段按 state["level"]
    每轮追加（见 prompts.learning_assistant_system_prompt），这里锁
    「叠加后」的总长。加入只读工作区契约后现状最长不足 430 字符，
    上限取 470 留出少量余量，防止未来指导词膨胀撑爆上下文预算。
    officecli 集成（T3-2）：助学角色追加 officecli_inspect 只读策略
    约 60 字符，现状最长 545，上限放宽到 580。
    六大功能计划 P1（功能 5 分步引导）：静态卡加分步引导约定 +
    _LEVEL_GUIDANCE 三档各加一句分步粒度，现状最长 593，上限
    放宽到 620，仍防无边界膨胀。
    """
    lengths = [
        len(learning_assistant_system_prompt(level))
        for level in (None, "basic", "advanced")
    ]
    assert max(lengths) <= 620


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


def test_workspace_enabled_prompts_require_grounded_workspace_inspection() -> None:
    """工作区问题必须核验真实文件能力，不能被解释成抽象工作方式。"""
    workspace_prompts = (
        ROLE_PROMPTS[AgentRole.SUPERVISOR],
        ROLE_PROMPTS[AgentRole.TEACHING_ASSISTANT],
        ROLE_PROMPTS[AgentRole.LEARNING_ASSISTANT],
        TOOL_ORCHESTRATION_SUPERVISOR_PROMPT,
    )

    for prompt in workspace_prompts:
        assert "workspace_info" in prompt
        assert "工作区" in prompt
        assert "绝对路径" in prompt
        assert "已授权" in prompt
        assert "不得猜测" in prompt

    assert "workspace_info" not in ROLE_PROMPTS[AgentRole.EVALUATOR]


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
