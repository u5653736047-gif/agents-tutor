"""统一 ReAct Agent 的核心行为测试。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from core.events import ErrorCode, EventType, RunError, RunEvent
from core.nodes.react_agent import ReActAgentNode
from core.state import AgentRole, create_initial_state
from core.tools.executor import ToolExecutor

MODEL_ERROR_SECRET = "secret=/srv/private/model-token"
UNKNOWN_TOOL_NAME = "unknown_tool"


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
        raise RuntimeError(MODEL_ERROR_SECRET)


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


@pytest.mark.parametrize("max_context_messages", [-1, 0, 2])
def test_react_agent_rejects_too_small_context_window(
    max_context_messages: int,
) -> None:
    with pytest.raises(ValueError, match="max_context_messages"):
        ReActAgentNode(
            role=AgentRole.SUPERVISOR,
            system_prompt="supervisor",
            model=ScriptedModel([]),
            max_context_messages=max_context_messages,
        )


def test_react_agent_trims_only_model_context_and_merges_extra_metrics() -> None:
    model = ScriptedModel([AIMessage(content="new answer")])
    agent = ReActAgentNode(
        role=AgentRole.TEACHING_ASSISTANT,
        system_prompt="system",
        model=model,
        max_context_messages=3,
    )
    history: list[BaseMessage] = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="latest question"),
        AIMessage(content="recent-1"),
        AIMessage(content="recent-2"),
    ]
    original = list(history)
    state = create_initial_state()
    state["messages"] = history
    state["extra"] = {"trace_id": "trace-1", "context_trimmed": 99}

    result = agent.run(state)

    assert [message.content for message in model.calls[0]] == [
        "system",
        "latest question",
        "recent-1",
        "recent-2",
    ]
    assert state["messages"] is history
    assert state["messages"] == original
    assert result.updates["messages"] == [result.messages[-1]]
    assert result.updates["extra"] == {
        "trace_id": "trace-1",
        "context_trimmed": 2,
        "context_message_count": 3,
    }


def test_react_agent_default_context_keeps_all_history_and_records_metrics() -> None:
    model = ScriptedModel([AIMessage(content="answer")])
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="system",
        model=model,
    )
    state = create_initial_state()
    state["messages"] = [HumanMessage(content="question")]
    state["extra"] = {"request_id": "request-1"}

    result = agent.run(state)

    assert [message.content for message in model.calls[0]] == ["system", "question"]
    assert result.updates["extra"] == {
        "request_id": "request-1",
        "context_trimmed": 0,
        "context_message_count": 1,
    }


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


def test_react_agent_replaces_unknown_tool_name_in_public_updates() -> None:
    secret_name = "missing-/srv/private/key"
    model = ScriptedModel(
        [
            AIMessage(content="", tool_calls=[tool_call(secret_name)]),
            AIMessage(content="fallback"),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="system",
        model=model,
    )

    result = agent.run(create_initial_state())

    assert secret_name not in str(result.updates)
    assert result.updates["tool_results"][0].tool_name == UNKNOWN_TOOL_NAME
    tool_events = [
        event
        for event in result.updates["events"]
        if event.event_type in {EventType.TOOL_STARTED, EventType.TOOL_COMPLETED}
    ]
    assert [event.tool_name for event in tool_events] == [
        UNKNOWN_TOOL_NAME,
        UNKNOWN_TOOL_NAME,
    ]


def test_react_agent_replaces_unknown_tool_name_in_v1_content_block() -> None:
    secret_name = "missing-/srv/private/v1-key"
    call = tool_call(secret_name)
    model = ScriptedModel(
        [
            AIMessage(
                content=[
                    {
                        "type": "tool_call",
                        "name": secret_name,
                        "args": {},
                        "id": "call-1",
                    }
                ],
                tool_calls=[call],
                response_metadata={"output_version": "v1"},
            ),
            AIMessage(content="fallback"),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="system",
        model=model,
    )

    result = agent.run(create_initial_state())

    persisted_message = result.updates["messages"][0]
    assert isinstance(persisted_message, AIMessage)
    assert isinstance(persisted_message.content, list)
    assert persisted_message.content[0]["name"] == UNKNOWN_TOOL_NAME
    assert secret_name not in str(result.updates)


def test_react_agent_preserves_registered_v1_tool_call_metadata() -> None:
    call = tool_call("double")
    content = [
        {"type": "text", "text": "working"},
        {
            "type": "tool_call",
            "name": "double",
            "args": {"value": 3},
            "id": "call-1",
        },
    ]
    response_metadata = {"output_version": "v1", "provider": "test"}
    additional_kwargs = {"provider_marker": "keep"}
    model = ScriptedModel(
        [
            AIMessage(
                content=content,
                tool_calls=[call],
                response_metadata=response_metadata,
                additional_kwargs=additional_kwargs,
            ),
            AIMessage(content="done"),
        ]
    )
    agent = ReActAgentNode(
        role=AgentRole.EVALUATOR,
        system_prompt="system",
        model=model,
        tool_executor=ToolExecutor([double]),
    )

    result = agent.run(create_initial_state())

    persisted_message = result.updates["messages"][0]
    assert isinstance(persisted_message, AIMessage)
    assert persisted_message.content == content
    assert persisted_message.response_metadata == response_metadata
    assert persisted_message.additional_kwargs == additional_kwargs
    assert result.updates["tool_results"][0].success is True
    assert result.updates["tool_results"][0].output == "6"


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
        message="模型调用失败",
        agent="evaluator",
    )
    assert MODEL_ERROR_SECRET not in result.error.model_dump_json()
    assert MODEL_ERROR_SECRET not in str(result.updates)
    assert result.updates["current_agent"] == "evaluator"
    assert result.updates["messages"] == []
    assert result.metadata["iterations"] == 1
    assert [event.event_type for event in result.updates["events"]] == [
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
    ]
    assert result.updates["events"][-1].success is False
    assert result.updates["events"][-1].error_code is ErrorCode.MODEL_CALL_FAILED
