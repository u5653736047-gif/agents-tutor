"""上下文窗口裁剪行为测试。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

import core.context as context_module
from core.context import ContextWindow, count_context_tokens, trim_message_history


def _tool_call(call_id: str | None) -> dict[str, object]:
    return {
        "name": "lookup",
        "args": {"query": call_id},
        "id": call_id,
        "type": "tool_call",
    }


def _content_token_count(messages: Sequence[BaseMessage]) -> int:
    """用正文字符数提供确定性的测试 Token 计数。"""
    return sum(len(str(message.content)) for message in messages)


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


def test_token_budget_trims_oldest_history() -> None:
    latest_human = HumanMessage(content="new")
    latest_answer = AIMessage(content="ok")
    history: list[BaseMessage] = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        latest_human,
        latest_answer,
    ]

    window = trim_message_history(
        history,
        max_tokens=5,
        token_counter=_content_token_count,
    )

    assert window.messages == (latest_human, latest_answer)
    assert window.trimmed_count == 2
    assert window.token_count == 5


def test_default_token_counter_is_conservative_and_counts_tool_metadata() -> None:
    short = [HumanMessage(content="你好")]
    long = [HumanMessage(content="你好世界")]
    plain_ai = AIMessage(content="")
    tool_ai = AIMessage(content="", tool_calls=[_tool_call("call-1")])
    tool_result = ToolMessage(content="ok", tool_call_id="call-1")
    short_id_result = ToolMessage(content="ok", tool_call_id="x")
    long_id_result = ToolMessage(content="ok", tool_call_id="call-id-long")

    short_count = count_context_tokens(short)

    assert isinstance(short_count, int)
    assert count_context_tokens(short) == short_count
    assert count_context_tokens(long) > short_count
    assert count_context_tokens([SystemMessage(content="system")]) > 0
    assert count_context_tokens([tool_ai]) > count_context_tokens([plain_ai])
    assert count_context_tokens([tool_ai, tool_result]) > count_context_tokens(
        [tool_ai]
    )
    assert count_context_tokens([long_id_result]) > count_context_tokens(
        [short_id_result]
    )


def test_default_token_counter_scans_history_linearly(monkeypatch: pytest.MonkeyPatch) -> None:
    original_counter = context_module.count_tokens_approximately
    observed_message_count = 0

    def recording_counter(
        messages: Sequence[BaseMessage],
        *,
        chars_per_token: float = 4.0,
    ) -> int:
        nonlocal observed_message_count
        observed_message_count += len(messages)
        return original_counter(messages, chars_per_token=chars_per_token)

    monkeypatch.setattr(
        context_module,
        "count_tokens_approximately",
        recording_counter,
    )
    history: list[BaseMessage] = [
        *(AIMessage(content=f"answer-{index}") for index in range(128)),
        HumanMessage(content="latest question"),
    ]

    window = trim_message_history(history, max_tokens=1_000_000)

    assert window.messages == tuple(history)
    assert observed_message_count <= len(history) * 2


@pytest.mark.parametrize(
    "invalid_value",
    [True, -1, 1.5, "1"],
    ids=["bool", "negative", "float", "string"],
)
def test_token_counter_rejects_invalid_return_value(
    invalid_value: object,
) -> None:
    def invalid_counter(_: Sequence[BaseMessage]) -> int:
        return cast(int, invalid_value)

    with pytest.raises(
        ValueError,
        match="token_counter must return a non-negative integer",
    ):
        trim_message_history(
            [HumanMessage(content="question")],
            max_tokens=10,
            token_counter=invalid_counter,
        )


def test_token_budget_always_keeps_latest_human_message() -> None:
    latest_human = HumanMessage(content="oversized question")

    window = trim_message_history(
        [latest_human],
        max_messages=None,
        max_tokens=1,
        token_counter=_content_token_count,
    )

    assert window.messages == (latest_human,)
    assert window.token_count == len("oversized question")


@pytest.mark.parametrize(
    ("max_tokens", "keep_group"),
    [(6, True), (5, False)],
)
def test_token_budget_keeps_or_drops_complete_tool_group(
    max_tokens: int,
    keep_group: bool,
) -> None:
    latest_human = HumanMessage(content="h")
    request = AIMessage(
        content="",
        tool_calls=[_tool_call("call-1"), _tool_call("call-2")],
    )
    first_result = ToolMessage(content="11", tool_call_id="call-1")
    second_result = ToolMessage(content="22", tool_call_id="call-2")
    final_answer = AIMessage(content="f")
    history: list[BaseMessage] = [
        latest_human,
        request,
        first_result,
        second_result,
        final_answer,
    ]

    window = trim_message_history(
        history,
        max_messages=None,
        max_tokens=max_tokens,
        token_counter=_content_token_count,
    )

    expected = tuple(history) if keep_group else (latest_human, final_answer)
    assert window.messages == expected
    assert (request in window.messages) is keep_group
    assert (first_result in window.messages) is keep_group
    assert (second_result in window.messages) is keep_group


def test_token_only_window_drops_orphan_tool_message() -> None:
    orphan = ToolMessage(content="orphan", tool_call_id="missing")
    human = HumanMessage(content="h")
    answer = AIMessage(content="a")

    window = trim_message_history(
        [orphan, human, answer],
        max_messages=None,
        max_tokens=20,
        token_counter=_content_token_count,
    )

    assert window.messages == (human, answer)
    assert window.trimmed_count == 1


@pytest.mark.parametrize(
    ("max_tokens", "expected_indexes"),
    [
        (100, (2, 3, 4)),
        (2, (2, 4)),
    ],
    ids=["message-limit", "token-limit"],
)
def test_message_and_token_limits_use_stricter_window(
    max_tokens: int,
    expected_indexes: tuple[int, ...],
) -> None:
    history: list[BaseMessage] = [
        HumanMessage(content="o"),
        AIMessage(content="a"),
        HumanMessage(content="n"),
        AIMessage(content="LLLL"),
        AIMessage(content="r"),
    ]

    window = trim_message_history(
        history,
        max_messages=3,
        max_tokens=max_tokens,
        token_counter=_content_token_count,
    )

    assert window.messages == tuple(history[index] for index in expected_indexes)


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_trim_message_history_rejects_non_positive_token_budget(
    max_tokens: int,
) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        trim_message_history([], max_messages=None, max_tokens=max_tokens)


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

    window = trim_message_history(history, max_messages=4)

    assert window.messages == (
        latest_human,
        request,
        first_result,
        second_result,
        final_answer,
    )
    assert window.trimmed_count == 2


def test_trim_drops_incomplete_multi_tool_call_group() -> None:
    latest_human = HumanMessage(content="look up both")
    request = AIMessage(
        content="",
        tool_calls=[_tool_call("call-1"), _tool_call("call-2")],
    )
    first_result = ToolMessage(content="one", tool_call_id="call-1")
    final_answer = AIMessage(content="partial answer")
    history: list[BaseMessage] = [
        HumanMessage(content="old"),
        latest_human,
        request,
        first_result,
        final_answer,
    ]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (latest_human, final_answer)
    assert window.trimmed_count == 3


@pytest.mark.parametrize(
    ("tool_calls", "result_id"),
    [
        ([_tool_call("valid"), _tool_call(None)], "valid"),
        ([_tool_call("same"), _tool_call("same")], "same"),
    ],
    ids=["missing-id", "duplicate-id"],
)
def test_trim_drops_tool_group_with_invalid_call_ids(
    tool_calls: list[dict[str, object]],
    result_id: str,
) -> None:
    latest_human = HumanMessage(content="look up both")
    request = AIMessage(content="", tool_calls=tool_calls)
    result = ToolMessage(content="one", tool_call_id=result_id)
    final_answer = AIMessage(content="partial answer")
    history: list[BaseMessage] = [
        latest_human,
        request,
        result,
        final_answer,
    ]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (latest_human, final_answer)
    assert window.trimmed_count == 2


def test_incomplete_group_is_removed_before_complete_group_capacity_check() -> None:
    latest_human = HumanMessage(content="latest")
    complete_request = AIMessage(
        content="",
        tool_calls=[_tool_call("complete")],
    )
    complete_result = ToolMessage(content="complete", tool_call_id="complete")
    incomplete_request = AIMessage(
        content="",
        tool_calls=[_tool_call("partial"), _tool_call("missing")],
    )
    partial_result = ToolMessage(content="partial", tool_call_id="partial")
    final_answer = AIMessage(content="final")
    history: list[BaseMessage] = [
        latest_human,
        complete_request,
        complete_result,
        incomplete_request,
        partial_result,
        final_answer,
    ]

    window = trim_message_history(history, max_messages=4)

    assert window.messages == (
        latest_human,
        complete_request,
        complete_result,
        final_answer,
    )
    assert window.trimmed_count == 2


def test_trim_drops_complete_tool_group_when_expansion_exceeds_hard_limit() -> None:
    latest_human = HumanMessage(content="look up four")
    request = AIMessage(
        content="",
        tool_calls=[_tool_call(f"call-{number}") for number in range(4)],
    )
    results = [
        ToolMessage(content=str(number), tool_call_id=f"call-{number}")
        for number in range(4)
    ]
    final_answer = AIMessage(content="combined")
    history: list[BaseMessage] = [
        HumanMessage(content="old"),
        latest_human,
        request,
        *results,
        final_answer,
    ]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == (latest_human, final_answer)
    assert len(window.messages) <= 4
    assert window.trimmed_count == 6


def test_latest_human_in_recent_tail_does_not_expand_hard_limit() -> None:
    latest_human = HumanMessage(content="latest")
    history: list[BaseMessage] = [
        HumanMessage(content="old"),
        AIMessage(content="old answer"),
        AIMessage(content="recent context"),
        latest_human,
        AIMessage(content="final answer"),
    ]

    window = trim_message_history(history, max_messages=3)

    assert window.messages == tuple(history[-3:])
    assert len(window.messages) <= 3
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
