"""为模型调用选择有限且结构完整的对话历史。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

MessageTokenCounter = Callable[[Sequence[BaseMessage]], int]


@dataclass(frozen=True, slots=True)
class ContextWindow:
    """一次裁剪产生的不可变消息窗口。"""

    messages: tuple[BaseMessage, ...]
    trimmed_count: int
    token_count: int | None = None


def count_context_tokens(messages: Sequence[BaseMessage]) -> int:
    """离线估算角色、正文及工具元数据，中文按一字符一 Token 保守计数。"""
    return count_tokens_approximately(messages, chars_per_token=1.0)


def trim_message_history(
    messages: Sequence[BaseMessage],
    max_messages: int | None = None,
    *,
    max_tokens: int | None = None,
    token_counter: MessageTokenCounter | None = None,
    prefix_messages: Sequence[BaseMessage] = (),
) -> ContextWindow:
    """保留最近历史，同时维持用户消息和工具调用关系。

    ``prefix_messages`` 只参与 Token 预算，不会写入返回的历史窗口。
    """
    if max_messages is not None and max_messages < 3:
        raise ValueError("max_messages must be at least 3")
    if max_tokens is not None and max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    history = list(messages)
    if not history:
        # 空历史：无需裁剪，但 token 预算仍要把前缀提示词算进去
        empty_token_count = (
            _count_tokens(
                token_counter or count_context_tokens,
                tuple(prefix_messages),
            )
            if max_tokens is not None
            else None
        )
        return ContextWindow(
            messages=(),
            trimmed_count=0,
            token_count=empty_token_count,
        )

    # 先按条数选出「最近 N 条」作候选集（不限制条数时全部入选）
    selected = (
        set(range(len(history)))
        if max_messages is None
        else set(range(max(0, len(history) - max_messages), len(history)))
    )
    # 最近一条用户消息是对话的落点，无论条数限制必须保留
    latest_human = next(
        (
            index
            for index in range(len(history) - 1, -1, -1)
            if isinstance(history[index], HumanMessage)
        ),
        None,
    )
    hard_limit = len(history) if max_messages is None else max_messages
    if latest_human is not None and latest_human not in selected:
        selected.add(latest_human)
        hard_limit += 1

    # 工具调用必须整组保留：缺父调用的孤儿结果剔除，组内成员不齐的整组剔除
    tool_groups, incomplete_parents, orphan_results = _tool_groups(history)
    selected.difference_update(orphan_results)
    # Tool Call 必须整组保留，缺失或越界则整组删除。
    for parent in incomplete_parents:
        selected.difference_update(tool_groups[parent])

    # 组内任一成员入选则补全整组；会突破条数上限时整组放弃（保持原子性）
    for parent, group in tool_groups.items():
        if parent in incomplete_parents or selected.isdisjoint(group):
            continue
        if len(selected) + len(group - selected) > hard_limit:
            selected.difference_update(group)
        else:
            selected.update(group)

    token_count: int | None = None
    # 还有 token 预算时，再以「原子组」为单位从新到旧往里装
    if max_tokens is not None:
        selected, token_count = _trim_by_tokens(
            history,
            selected,
            tool_groups,
            latest_human,
            max_tokens,
            token_counter,
            prefix_messages,
        )

    kept = tuple(history[index] for index in sorted(selected))
    if max_tokens is not None and token_count is None:
        token_count = _count_tokens(
            token_counter or count_context_tokens,
            (*prefix_messages, *kept),
        )
    return ContextWindow(
        messages=kept,
        trimmed_count=len(history) - len(kept),
        token_count=token_count,
    )


def _trim_by_tokens(
    history: Sequence[BaseMessage],
    selected: set[int],
    tool_groups: dict[int, set[int]],
    latest_human: int | None,
    max_tokens: int,
    token_counter: MessageTokenCounter | None,
    prefix_messages: Sequence[BaseMessage],
) -> tuple[set[int], int | None]:
    """从最近原子消息组向前选择；最近用户消息即使超预算也保持必留。"""
    mandatory = (
        {latest_human}
        if latest_human is not None and latest_human in selected
        else set()
    )
    kept = set(mandatory)
    grouped_indexes: set[int] = set()
    units: list[set[int]] = []
    for group in tool_groups.values():
        if group <= selected:
            units.append(group)
            grouped_indexes.update(group)
    units.extend({index} for index in selected - grouped_indexes)
    # 按组内最新消息的下标倒序排列 = 时间从近到远
    units.sort(key=max, reverse=True)

    if token_counter is None:
        return _trim_by_additive_default(
            history,
            mandatory,
            units,
            max_tokens,
            prefix_messages,
        )

    for unit in units:
        if not unit.isdisjoint(mandatory):
            continue
        candidate = kept | unit
        candidate_messages = (
            *prefix_messages,
            *(history[index] for index in sorted(candidate)),
        )
        if _count_tokens(token_counter, candidate_messages) > max_tokens:
            break  # 超预算即停止：这里就是从新到旧的截断点
        kept.update(unit)
    return kept, None


def _trim_by_additive_default(
    history: Sequence[BaseMessage],
    mandatory: set[int],
    units: Sequence[set[int]],
    max_tokens: int,
    prefix_messages: Sequence[BaseMessage],
) -> tuple[set[int], int]:
    """利用默认逐消息可加计数器在线性扫描中选择原子单元。"""
    kept = set(mandatory)
    mandatory_messages = tuple(
        history[index] for index in sorted(mandatory)
    )
    # 默认估算器逐消息向上取整可累加：先数必留消息，再逐组相加避免重复扫描
    running_count = count_context_tokens(
        (*prefix_messages, *mandatory_messages)
    )

    for unit in units:
        if not unit.isdisjoint(mandatory):
            continue
        unit_messages = tuple(history[index] for index in sorted(unit))
        # 默认估算器逐消息向上取整，完整列表等于各消息计数之和。
        unit_count = count_context_tokens(unit_messages)
        if running_count + unit_count > max_tokens:
            break
        kept.update(unit)
        running_count += unit_count
    return kept, running_count


def _count_tokens(
    token_counter: MessageTokenCounter,
    messages: Sequence[BaseMessage],
) -> int:
    """验证可注入计数器的稳定返回边界。"""
    token_count = token_counter(messages)
    if (
        isinstance(token_count, bool)
        or not isinstance(token_count, int)
        or token_count < 0
    ):
        raise ValueError("token_counter must return a non-negative integer")
    return token_count


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
                    invalid_parents.add(index)  # 缺 id 的调用：组不完整，判无效
                    continue
                normalized_id = str(call_id)
                if normalized_id in expected_ids[index]:
                    invalid_parents.add(index)  # 重复 id 的调用：同样判无效
                    continue
                expected_ids[index].add(normalized_id)
                call_parents[normalized_id] = index
            continue
        if not isinstance(message, ToolMessage):
            continue
        normalized_id = str(message.tool_call_id)
        parent = call_parents.get(normalized_id)
        if parent is None:
            orphan_results.add(index)  # 找不到父调用的结果消息：孤儿，剔除
            continue
        groups[parent].add(index)
        observed_ids[parent].add(normalized_id)

    # 不完整父消息 = 调用本身无效 + 有调用但结果没配齐；这些组整组剔除
    incomplete_parents = invalid_parents | {
        parent
        for parent, expected in expected_ids.items()
        if not expected or not expected.issubset(observed_ids[parent])
    }
    return groups, incomplete_parents, orphan_results


__all__ = [
    "ContextWindow",
    "MessageTokenCounter",
    "count_context_tokens",
    "trim_message_history",
]
