"""统一 ReAct Agent 的核心行为测试。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool

from core.events import ErrorCode, EventType, RunError, RunEvent
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


@pytest.mark.parametrize("max_iterations", [0, -1])
def test_react_agent_rejects_non_positive_iteration_limit(
    max_iterations: int,
) -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        ReActAgentNode(
            role=AgentRole.SUPERVISOR,
            system_prompt="supervisor",
            model=ScriptedModel([]),
            max_iterations=max_iterations,
        )


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
    assert [event.event_type for event in result.updates["events"]] == [
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
    ]
    assert {event.session_id for event in result.updates["events"]} == {None}
    assert result.updates["events"][-1].success is True
    assert result.updates["events"][-1].duration_ms is not None


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


def test_react_agent_emits_safe_ordered_events_after_history() -> None:
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call("double")]),
            AIMessage(content="sensitive answer"),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.TEACHING_ASSISTANT,
        system_prompt="你是助教。",
        model=model,
        tool_executor=ToolExecutor([double]),
    )
    state = create_initial_state(session_id="session-1")
    state["events"] = [
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=7,
            session_id="session-1",
        )
    ]

    result = agent.run(state)
    events = result.updates["events"]

    assert [event.event_type for event in events] == [
        EventType.AGENT_STARTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.AGENT_COMPLETED,
    ]
    assert [event.sequence for event in events] == [8, 9, 10, 11]
    assert {event.session_id for event in events} == {"session-1"}
    assert events[1].tool_name == "double"
    assert events[2].success is True
    assert events[2].duration_ms is not None
    assert events[3].success is True
    assert events[3].duration_ms is not None
    safe_fields = {
        "event_type",
        "sequence",
        "session_id",
        "agent",
        "tool_name",
        "success",
        "duration_ms",
        "error_code",
    }
    assert all(set(event.model_dump()) == safe_fields for event in events)


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

    assert result.error == RunError(
        error_code=ErrorCode.REACT_ITERATION_LIMIT,
        message="ReAct 循环达到最大轮数：2",
        agent="teaching_assistant",
    )
    assert len(result.updates["tool_results"]) == 2
    assert result.metadata["iterations"] == 2
    assert result.updates["events"][-1].event_type is EventType.AGENT_COMPLETED
    assert result.updates["events"][-1].success is False
    assert (
        result.updates["events"][-1].error_code
        is ErrorCode.REACT_ITERATION_LIMIT
    )


def test_react_agent_returns_model_error() -> None:
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="你是评价助手。",
        model=FailingModel(),
    )

    result = agent.run(create_initial_state())

    assert result.error == RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="模型调用失败：模型不可用",
        agent="evaluator",
    )
    assert result.updates["current_agent"] == "evaluator"
    assert result.updates["messages"] == []
    assert result.metadata["iterations"] == 1
    assert [event.event_type for event in result.updates["events"]] == [
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
    ]
    assert result.updates["events"][-1].success is False
    assert result.updates["events"][-1].error_code is ErrorCode.MODEL_CALL_FAILED
