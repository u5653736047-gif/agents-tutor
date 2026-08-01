"""StateGraph 组装与条件路由（任务 1.1.2 / 1.1.3 / 1.1.4）.

- 1.1.3：Supervisor 条件边支持返回 ``list[Send]`` 实现并行 fan-out：
  节点写入 ``extra["fan_out"]`` 的是纯数据分派计划（可 JSON 序列化，
  兼容 checkpointer），条件边读出后重建 Send 对象。
- 1.1.4：``build_graph`` 支持注入 checkpointer 与 IntentRouter；
  传入 checkpointer 时 Supervisor 开启确认闸门（HITL）。
- 多轮对话：入口 ``ingest`` 节点在「上一任务已终结 + 收到新用户消息」
  时重建 TaskContext，避免残留的终结状态让新请求被直接 END 吞掉。
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from core.intent_router import IntentRouter
from core.nodes import (
    EvaluatorNode,
    LearningAssistantNode,
    SupervisorNode,
    TeachingAssistantNode,
)
from core.state import AgentRole, AgentState, TaskContext, TaskStatus

# 任务终结状态：处于这些状态的 task_context 视为「上一轮已结束」
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def _message_text(message: HumanMessage) -> str:
    """提取消息文本内容（兼容 str 与多模态 list 内容块）."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def ingest_request(state: AgentState) -> dict[str, Any]:
    """入口预处理节点：为新用户请求重建 TaskContext.

    触发条件：最新一条消息是尚未处理的 HumanMessage，且当前任务不存在
    或已终结（COMPLETED/FAILED/CANCELLED）。此时基于消息文本创建新的
    TaskContext（意图留空，交由 Supervisor 的路由器分类）；否则不动状态。

    没有该节点时，同一 thread 内上一轮残留的终结 task_context 会让
    Supervisor 在 decide 时直接 END，新用户消息永远得不到处理。
    """
    messages = state.get("messages") or []
    if not messages or not isinstance(messages[-1], HumanMessage):
        return {}
    task = state.get("task_context")
    if task is not None and task.status not in _TERMINAL_STATUSES:
        return {}
    return {"task_context": TaskContext(description=_message_text(messages[-1]))}


def _rebuild_send(item: dict[str, Any]) -> Send:
    """把纯数据分派计划项重建为 Send（task_context 字典还原为 TaskContext）."""
    payload = dict(item["payload"])
    payload["task_context"] = TaskContext.model_validate(payload["task_context"])
    return Send(item["node"], payload)


def route_by_next_agent(state: AgentState) -> str | list[Send]:
    """条件边路由：优先返回并行分派计划，否则按 ``State.next_agent`` 路由.

    条件边在 Supervisor 节点返回后执行，能读到节点写回 ``extra["fan_out"]``
    的纯数据分派计划（dict 列表），重建 Send 列表返回；fan-out 分支由
    ``next_agent`` 哨兵值 END 兜底。
    """
    fan_out = (state.get("extra") or {}).get("fan_out")
    if isinstance(fan_out, list) and fan_out:
        return [_rebuild_send(item) for item in fan_out]
    return state.get("next_agent") or END


def build_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    router: IntentRouter | None = None,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """构建多智能体 StateGraph.

    入口 ``ingest`` 节点完成多轮请求预处理后进入 Supervisor；Supervisor
    通过条件边按意图分派任务（多子任务时返回 ``list[Send]`` 并行 fan-out）；
    子 Agent 执行完成后通过普通边回到 Supervisor 聚合，全部完成时路由到 END。

    Args:
        checkpointer: 传入时编译期附加检查点，Supervisor 开启确认闸门
            （HITL interrupt 依赖 checkpointer）；不传则行为与无 HITL 完全一致。
        router: 注入 Supervisor 的意图分类路由，默认规则路由。
    """
    graph = StateGraph(AgentState)

    graph.add_node("ingest", ingest_request)
    graph.add_node(
        "supervisor",
        SupervisorNode(
            router=router,
            require_confirmation=checkpointer is not None,
        ),
    )
    graph.add_node("teaching_assistant", TeachingAssistantNode())
    graph.add_node("learning_assistant", LearningAssistantNode())
    graph.add_node("evaluator", EvaluatorNode())

    graph.set_entry_point("ingest")
    graph.add_edge("ingest", "supervisor")

    node_names = {role.value for role in AgentRole}
    path_map: dict[Hashable, str] = {}
    for name in node_names:
        path_map[name] = name
    path_map[END] = END
    graph.add_conditional_edges("supervisor", route_by_next_agent, path_map)

    for name in node_names - {AgentRole.SUPERVISOR.value}:
        graph.add_edge(name, "supervisor")

    return graph.compile(checkpointer=checkpointer)
