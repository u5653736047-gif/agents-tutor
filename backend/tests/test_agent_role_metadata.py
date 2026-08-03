"""助手消息携带「产出 Agent 角色」元数据的测试。

验收核心：助手消息经 LangGraph SQLite checkpointer 持久化、进程重建
（关闭连接、以新连接重建图实例）后，get_history() 读出的消息仍能通过
core.state.message_agent_role() 恢复角色——即 additional_kwargs 的
序列化往返保真。

与实现一一对应的设计约定：
- 注入点：CollaborativeAgentGraph._wrap（消息写入 state["messages"] 的
  唯一闸口）。因此以下测试全部走真实图执行（而非直接调用
  ReActAgentNode），确保覆盖注入路径；ReActAgentNode 的单元语义不变。
- 只给 AIMessage 注入角色；HumanMessage（用户输入/任务描述）与
  ToolMessage（工具返回）不带角色。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.tools import tool

from core.graph_builder import CollaborativeAgentGraph
from core.persistence import open_sqlite_checkpointer
from core.state import (
    AGENT_ROLE_METADATA_KEY,
    AgentRole,
    message_agent_role,
    with_agent_role,
)


class ScriptedModel:
    """按图执行顺序返回预设模型消息（确定性替身，不依赖真实模型）。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)

    def bind_tools(self, tools: Sequence[object]) -> ScriptedModel:
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        return self.responses.pop(0)


@tool
def double(value: int) -> int:
    """返回输入数字的两倍。"""
    return value * 2


def _handoff_response(target: str = "teaching_assistant") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "handoff",
                "args": {"target": target},
                "id": "role-handoff",
                "type": "tool_call",
            }
        ],
    )


def _plan_response() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_task_plan",
                "args": {
                    "steps": [
                        {
                            "sequence": 1,
                            "description": "讲解梯度下降",
                            "target_agent": "teaching_assistant",
                        },
                        {
                            "sequence": 2,
                            "description": "检查讲解准确性",
                            "target_agent": "evaluator",
                        },
                    ]
                },
                "id": "role-aggregation-plan",
                "type": "tool_call",
            }
        ],
    )


def _ai_messages(messages: Sequence[BaseMessage]) -> list[AIMessage]:
    return [message for message in messages if isinstance(message, AIMessage)]


def test_single_agent_answer_carries_supervisor_role() -> None:
    """普通单 Agent 回答 → 该 Agent 角色（入口即 Supervisor）。"""
    model = ScriptedModel([AIMessage(content="直接回答")])
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("你好")

    ai = _ai_messages(result["messages"])
    assert [message_agent_role(message) for message in ai] == [
        AgentRole.SUPERVISOR
    ]
    # 对外内容不变，只是新增元数据
    assert ai[0].content == "直接回答"


def test_supervisor_aggregated_answer_carries_supervisor_role() -> None:
    """Supervisor 聚合多子任务结果的最终回答 → supervisor（实际产出者）。"""
    model = ScriptedModel(
        [
            _plan_response(),
            AIMessage(content="计划已创建"),
            AIMessage(content="教学结果：梯度下降沿负梯度更新"),
            AIMessage(content="评价结果：讲解准确"),
            AIMessage(content="统一回答：讲解内容及评价结论"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请讲解并检查梯度下降")

    # 无工具调用的助手消息依次为：计划已创建(supervisor)、教学结果
    # (teaching_assistant)、评价结果(evaluator)、聚合回答(supervisor)。
    # 聚合回答的 content 由 _replace_terminal_ai_output 改写，但
    # additional_kwargs 保留，因此角色仍是 supervisor。
    roles = [
        message_agent_role(message)
        for message in result["messages"]
        if isinstance(message, AIMessage) and not message.tool_calls
    ]
    assert roles == [
        AgentRole.SUPERVISOR,
        AgentRole.TEACHING_ASSISTANT,
        AgentRole.EVALUATOR,
        AgentRole.SUPERVISOR,
    ]
    assert result["messages"][-1].content == "统一回答：讲解内容及评价结论"


def test_multi_round_handoff_keeps_each_role_distinct() -> None:
    """多轮 handoff：每条助手消息携带各自产出者的角色，互不污染。"""
    model = ScriptedModel(
        [
            _handoff_response(),
            AIMessage(content="任务已分派"),
            AIMessage(content="教学结果"),
            AIMessage(content="最终汇总"),
        ]
    )
    graph = CollaborativeAgentGraph(model=model)

    result = graph.run("请解释梯度下降")

    # handoff 请求(supervisor) → 任务已分派(supervisor) →
    # 教学结果(teaching_assistant) → 最终汇总(supervisor)
    ai = _ai_messages(result["messages"])
    assert [message_agent_role(message) for message in ai] == [
        AgentRole.SUPERVISOR,
        AgentRole.SUPERVISOR,
        AgentRole.TEACHING_ASSISTANT,
        AgentRole.SUPERVISOR,
    ]


def test_sqlite_checkpointer_round_trip_restores_role(tmp_path: Path) -> None:
    """SQLite checkpointer 持久化 → 连接关闭/新实例重建 → get_history 仍能读出角色。

    这是验收核心：模拟进程重建（连接关闭后以新连接、新图实例重载），
    验证 additional_kwargs 的角色元数据经 msgpack 序列化往返不失真；
    并验证重建后的新实例继续执行时，新消息同样携带角色。
    """
    checkpoint_path = tmp_path / "nested" / "role-checkpoints.sqlite"
    session_id = "role-round-trip"
    user_id = "user-1"

    # 第一段：写入并持久化
    with open_sqlite_checkpointer(checkpoint_path) as first_saver:
        first_graph = CollaborativeAgentGraph(
            model=ScriptedModel([AIMessage(content="第一轮回答")]),
            checkpointer=first_saver,
        )
        first_graph.run("第一问", session_id, user_id)

    # 第二段：连接已关闭，以新连接 + 全新图实例重载历史（进程重建模拟）
    with open_sqlite_checkpointer(checkpoint_path) as second_saver:
        second_graph = CollaborativeAgentGraph(
            model=ScriptedModel([AIMessage(content="第二轮回答")]),
            checkpointer=second_saver,
        )
        history = second_graph.get_history(session_id, user_id)

    ai = _ai_messages(history)
    assert len(ai) == 1
    assert ai[0].content == "第一轮回答"
    assert message_agent_role(ai[0]) == AgentRole.SUPERVISOR

    # 第三段：重建后的实例继续新轮次，跨轮历史与新增消息都携带角色
    with open_sqlite_checkpointer(checkpoint_path) as third_saver:
        third_graph = CollaborativeAgentGraph(
            model=ScriptedModel([AIMessage(content="第二轮回答")]),
            checkpointer=third_saver,
        )
        third_graph.run("第二问", session_id, user_id)
        history = third_graph.get_history(session_id, user_id)

    ai = _ai_messages(history)
    assert [message.content for message in ai] == ["第一轮回答", "第二轮回答"]
    assert [message_agent_role(message) for message in ai] == [
        AgentRole.SUPERVISOR,
        AgentRole.SUPERVISOR,
    ]


def test_human_and_tool_messages_carry_no_role() -> None:
    """HumanMessage（用户输入）与 ToolMessage（工具返回）不注入角色。"""
    call = {
        "name": "double",
        "args": {"value": 3},
        "id": "role-tool-call",
        "type": "tool_call",
    }
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="结果是 6"),
        ]
    )
    graph = CollaborativeAgentGraph(
        model=model,
        tools=[double],
        tool_permissions={"double": {AgentRole.SUPERVISOR}},
    )

    result = graph.run("算一下")

    humans = [m for m in result["messages"] if isinstance(m, HumanMessage)]
    tools = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(humans) == 1  # 用户输入
    assert len(tools) == 1  # double 工具返回
    assert all(
        AGENT_ROLE_METADATA_KEY not in message.additional_kwargs
        for message in [*humans, *tools]
    )
    assert all(message_agent_role(message) is None for message in [*humans, *tools])
    # 对照：同一轮里的助手消息仍带角色
    assert message_agent_role(_ai_messages(result["messages"])[-1]) == (
        AgentRole.SUPERVISOR
    )


def test_with_agent_role_returns_copy_and_preserves_existing_kwargs() -> None:
    """注入函数不改原对象，且保留消息既有的 additional_kwargs 与内容。"""
    original = AIMessage(content="回答", additional_kwargs={"provider": "x"})

    tagged = with_agent_role(original, AgentRole.EVALUATOR)

    assert tagged.additional_kwargs == {
        "provider": "x",
        AGENT_ROLE_METADATA_KEY: AgentRole.EVALUATOR.value,
    }
    assert tagged.content == "回答"
    # 原对象未被就地修改（副本语义，避免污染模型返回对象）
    assert original.additional_kwargs == {"provider": "x"}


def test_message_agent_role_tolerates_missing_or_invalid_metadata() -> None:
    """读取端宽容：缺失或非法元数据一律返回 None，不抛异常。"""
    assert message_agent_role(HumanMessage(content="hi")) is None
    assert message_agent_role(AIMessage(content="hi")) is None
    invalid = AIMessage(
        content="hi",
        additional_kwargs={AGENT_ROLE_METADATA_KEY: "ghost_role"},
    )
    assert message_agent_role(invalid) is None
    # 键存在但值非字符串（脏数据）同样返回 None
    non_string = AIMessage(
        content="hi",
        additional_kwargs={AGENT_ROLE_METADATA_KEY: 42},
    )
    assert message_agent_role(non_string) is None
