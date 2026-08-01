"""统一 ReAct Agent 的核心行为测试。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool

from core.nodes.react_agent import ReActAgentNode
from core.state import AgentRole, create_initial_state
from core.tools.executor import ToolExecutor


class ScriptedModel:
    """按顺序返回预设消息，便于验证 ReAct 循环。"""

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list[BaseMessage]] = []

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0)


class FailingModel:
    """模拟不可恢复的模型调用错误。"""

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        raise RuntimeError("模型不可用")


@tool
def double(value: int) -> int:
    """返回输入数字的两倍。"""
    return value * 2


def tool_call(name: str, call_id: str = "call-1") -> dict[str, object]:
    """创建与 LangChain AIMessage 兼容的工具调用。"""
    args = {"value": 3} if name == "double" else {}
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_react_agent_returns_direct_answer_without_tool() -> None:
    model = ScriptedModel([AIMessage(content="直接回答")])
    agent = ReActAgentNode(
        role=AgentRole.TEACHING_ASSISTANT,
        system_prompt="你是助教。",
        model=model,
    )

    result = agent.run(create_initial_state())

    assert result.error is None
    assert result.updates["current_agent"] == "teaching_assistant"
    assert [message.content for message in result.updates["messages"]] == ["直接回答"]
    assert result.updates["tool_results"] == []
    assert result.metadata["iterations"] == 1
    assert model.calls[0][0].content == "你是助教。"


def test_react_agent_feeds_tool_observation_back_to_model() -> None:
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("double")]),
            AIMessage(content="结果是 6"),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.TEACHING_ASSISTANT,
        system_prompt="你是助教。",
        model=model,
        tool_executor=ToolExecutor([double]),
    )

    result = agent.run(create_initial_state())

    assert result.error is None
    assert len(model.calls) == 2
    assert any(
        isinstance(message, ToolMessage) and message.content == "6"
        for message in model.calls[1]
    )
    assert len(result.updates["tool_results"]) == 1
    assert result.updates["messages"][-1].content == "结果是 6"
    assert result.metadata["iterations"] == 2


def test_react_agent_stops_at_iteration_limit() -> None:
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("double", "call-1")]),
            AIMessage(content="", tool_calls=[tool_call("double", "call-2")]),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.TEACHING_ASSISTANT,
        system_prompt="你是助教。",
        model=model,
        tool_executor=ToolExecutor([double]),
        max_iterations=2,
    )

    result = agent.run(create_initial_state())

    assert result.error == "ReAct 循环达到最大轮数：2"
    assert len(result.updates["tool_results"]) == 2
    assert result.metadata["iterations"] == 2


def test_react_agent_returns_model_error() -> None:
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="你是评价助手。",
        model=FailingModel(),
    )

    result = agent.run(create_initial_state())

    assert result.error == "模型调用失败：模型不可用"
    assert result.updates["current_agent"] == "evaluator"
    assert result.updates["messages"] == []
    assert result.metadata["iterations"] == 1
