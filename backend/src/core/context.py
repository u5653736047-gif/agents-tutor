"""为模型调用选择有限且结构完整的对话历史。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage


@dataclass(frozen=True, slots=True)
class ContextWindow:
    """一次裁剪产生的不可变消息窗口。"""

    messages: tuple[BaseMessage, ...]
    trimmed_count: int


def trim_message_history(
    messages: Sequence[BaseMessage],
    max_messages: int,
) -> ContextWindow:
    """保留最近历史，同时维持用户消息和工具调用关系。"""
    if max_messages < 3:
        raise ValueError("max_messages must be at least 3")

    history = list(messages)
    if not history:
        return ContextWindow(messages=(), trimmed_count=0)

    selected = set(range(max(0, len(history) - max_messages), len(history)))
    latest_human = next(
        (
            index
            for index in range(len(history) - 1, -1, -1)
            if isinstance(history[index], HumanMessage)
        ),
        None,
    )
    hard_limit = max_messages
    if latest_human is not None and latest_human not in selected:
        selected.add(latest_human)
        hard_limit += 1

    tool_groups, incomplete_parents, orphan_results = _tool_groups(history)
    selected.difference_update(orphan_results)
    # Tool Call 必须整组保留，缺失或越界则整组删除。
    for parent in incomplete_parents:
        selected.difference_update(tool_groups[parent])

    for parent, group in tool_groups.items():
        if parent in incomplete_parents or selected.isdisjoint(group):
            continue
        if len(selected) + len(group - selected) > hard_limit:
            selected.difference_update(group)
        else:
            selected.update(group)

    kept = tuple(history[index] for index in sorted(selected))
    return ContextWindow(
        messages=kept,
        trimmed_count=len(history) - len(kept),
    )


def _tool_groups(
    messages: Sequence[BaseMessage],
) -> tuple[dict[int, set[int]], set[int], set[int]]:
    """收集工具调用原子组、不完整父消息和孤立结果。"""
    call_parents: dict[str, int] = {}
    expected_ids: dict[int, set[str]] = {}
    observed_ids: dict[int, set[str]] = {}
    groups: dict[int, set[int]] = {}
    invalid_parents: set[int] = set()
    orphan_results: set[int] = set()

    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            groups[index] = {index}
            expected_ids[index] = set()
            observed_ids[index] = set()
            for tool_call in message.tool_calls:
                call_id = tool_call.get("id")
                if not call_id:
                    invalid_parents.add(index)
                    continue
                normalized_id = str(call_id)
                if normalized_id in expected_ids[index]:
                    invalid_parents.add(index)
                    continue
                expected_ids[index].add(normalized_id)
                call_parents[normalized_id] = index
            continue
        if not isinstance(message, ToolMessage):
            continue
        normalized_id = str(message.tool_call_id)
        parent = call_parents.get(normalized_id)
        if parent is None:
            orphan_results.add(index)
            continue
        groups[parent].add(index)
        observed_ids[parent].add(normalized_id)

    incomplete_parents = invalid_parents | {
        parent
        for parent, expected in expected_ids.items()
        if not expected or not expected.issubset(observed_ids[parent])
    }
    return groups, incomplete_parents, orphan_results


__all__ = ["ContextWindow", "trim_message_history"]
