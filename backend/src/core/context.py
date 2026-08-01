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
    if latest_human is not None:
        selected.add(latest_human)

    tool_parents, tool_children = _tool_relationships(history)
    for index in tuple(selected):
        if not isinstance(history[index], ToolMessage):
            continue
        parent = tool_parents.get(index)
        if parent is None:
            selected.remove(index)
            continue
        # max_messages 是目标窗口；完整工具组和最新用户消息可使结果略大。
        selected.add(parent)
        selected.update(tool_children[parent])

    kept = tuple(history[index] for index in sorted(selected))
    return ContextWindow(
        messages=kept,
        trimmed_count=len(history) - len(kept),
    )


def _tool_relationships(
    messages: Sequence[BaseMessage],
) -> tuple[dict[int, int], dict[int, set[int]]]:
    """将工具结果关联到此前声明对应调用 ID 的 AI 消息。"""
    call_parents: dict[str, int] = {}
    tool_parents: dict[int, int] = {}
    tool_children: dict[int, set[int]] = {}

    for index, message in enumerate(messages):
        if isinstance(message, AIMessage):
            for tool_call in message.tool_calls:
                call_id = tool_call.get("id")
                if call_id:
                    call_parents[str(call_id)] = index
            continue
        if not isinstance(message, ToolMessage):
            continue
        parent = call_parents.get(str(message.tool_call_id))
        if parent is None:
            continue
        tool_parents[index] = parent
        tool_children.setdefault(parent, set()).add(index)

    return tool_parents, tool_children


__all__ = ["ContextWindow", "trim_message_history"]
