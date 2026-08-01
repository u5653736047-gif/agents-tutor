"""上下文窗口裁剪行为测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from core.context import ContextWindow, trim_message_history


def _tool_call(call_id: str) -> dict[str, object]:
    return {
        "name": "lookup",
        "args": {"query": call_id},
        "id": call_id,
        "type": "tool_call",
    }


@pytest.mark.parametrize("max_messages", [-1, 0, 2])
def test_trim_message_history_rejects_too_small_window(max_messages: int) -> None:
    with pytest.raises(ValueError, match="max_messages"):
        trim_message_history([], max_messages)


def test_short_history_returns_immutable_copy_without_trimming() -> None:
    history: list[BaseMessage] = [
        HumanMessage(content="question"),
        AIMessage(content="answer"),
    ]

    window = trim_message_history(history, max_messages=3)

    assert isinstance(window, ContextWindow)
    assert window.messages == tuple(history)
    assert window.trimmed_count == 0
    assert window.messages is not history
    with pytest.raises(FrozenInstanceError):
        window.trimmed_count = 1


def test_short_history_drops_leading_orphan_tool_message() -> None:
    orphan = ToolMessage(content="orphan", tool_call_id="missing")
    human = HumanMessage(content="question")
    answer = AIMessage(content="answer")
    history: list[BaseMessage] = [orphan, human, answer]
    original = list(history)

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (human, answer)
    assert window.trimmed_count == 1
    assert history == original


def test_long_history_keeps_recent_messages_and_latest_human() -> None:
    latest_human = HumanMessage(content="latest question")
    history: list[BaseMessage] = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        latest_human,
        AIMessage(content="recent-1"),
        AIMessage(content="recent-2"),
        AIMessage(content="recent-3"),
    ]
    original = list(history)

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (
        latest_human,
        history[3],
        history[4],
        history[5],
    )
    assert window.trimmed_count == 2
    assert history == original


def test_trim_keeps_complete_multi_tool_call_group() -> None:
    latest_human = HumanMessage(content="look up both")
    request = AIMessage(
        content="",
        tool_calls=[_tool_call("call-1"), _tool_call("call-2")],
    )
    first_result = ToolMessage(content="one", tool_call_id="call-1")
    second_result = ToolMessage(content="two", tool_call_id="call-2")
    final_answer = AIMessage(content="combined")
    history: list[BaseMessage] = [
        HumanMessage(content="old"),
        AIMessage(content="old answer"),
        latest_human,
        request,
        first_result,
        second_result,
        final_answer,
    ]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (
        latest_human,
        request,
        first_result,
        second_result,
        final_answer,
    )
    assert window.trimmed_count == 2


def test_trim_drops_orphan_tool_message_instead_of_starting_with_it() -> None:
    orphan = ToolMessage(content="orphan", tool_call_id="missing")
    latest_human = HumanMessage(content="latest")
    history: list[BaseMessage] = [
        HumanMessage(content="old"),
        AIMessage(content="old answer"),
        orphan,
        latest_human,
        AIMessage(content="answer"),
    ]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (latest_human, history[-1])
    assert window.trimmed_count == 3
    assert not isinstance(window.messages[0], ToolMessage)
