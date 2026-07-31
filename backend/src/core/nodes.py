"""Agent 节点（Node）抽象（任务 1.1.2）.

每个 Agent 角色（协调/助教/助学/评价）作为 StateGraph 的一个节点，
节点内部遵循「思考 → 决策 → 执行 → 观察」循环，节点间通过 State 传递上下文，
由 Edge 条件路由决定下一节点。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.graph import END

from core.state import AgentRole, AgentState, TaskContext, TaskStatus


class BaseAgentNode(ABC):
    """LangGraph 节点基类：将一次节点调用编排为 思考→决策→执行→观察 循环.

    Usage::

        graph.add_node(agent.name, agent)

    Args:
        role: 节点对应的 Agent 角色，``role.value`` 即图节点名称。
    """

    def __init__(self, role: AgentRole) -> None:
        self.role = role

    @property
    def name(self) -> str:
        """图节点名称，与 ``AgentRole`` 值保持一致."""
        return self.role.value

    def __call__(self, state: AgentState) -> dict[str, Any]:
        """LangGraph 节点入口，编排四阶段循环并返回 State 的部分更新."""
        plan = self.think(state)
        action = self.decide(state, plan)
        result = self.execute(state, action)
        return self.observe(state, action, result)

    @abstractmethod
    def think(self, state: AgentState) -> Any:
        """思考：基于当前 State 生成处理计划（后续接入 LLM 推理）. """

    @abstractmethod
    def decide(self, state: AgentState, plan: Any) -> str:
        """决策：选择下一步动作（回复 / 调用工具 / 转交其他 Agent）. """

    @abstractmethod
    def execute(self, state: AgentState, action: str) -> Any:
        """执行：执行所选动作，生成回复内容或触发工具调用. """

    @abstractmethod
    def observe(self, state: AgentState, action: str, result: Any) -> dict[str, Any]:
        """观察：将执行结果写回 State，返回图节点需要更新的字段. """


_INTENT_ROUTING: dict[str, str] = {
    "teach": AgentRole.TEACHING_ASSISTANT.value,
    "learn": AgentRole.LEARNING_ASSISTANT.value,
    "evaluate": AgentRole.EVALUATOR.value,
}


class SupervisorNode(BaseAgentNode):
    """协调 Agent 节点：按意图将任务路由给对应子 Agent，任务完成后结束.

    说明：本子任务仅搭建路由骨架；LLM 意图识别与任务分解在 1.1.3 实现。
    """

    def __init__(self) -> None:
        super().__init__(AgentRole.SUPERVISOR)

    def think(self, state: AgentState) -> str:
        task = state.get("task_context")
        return f"分析意图：{task.intent if task else 'unknown'}"

    def decide(self, state: AgentState, plan: str) -> str:
        """返回下一节点名；任务全部完成时返回 END."""
        task = state.get("task_context")
        if task and task.status == TaskStatus.COMPLETED:
            return END
        intent = task.intent if task else ""
        return _INTENT_ROUTING.get(intent, AgentRole.TEACHING_ASSISTANT.value)

    def execute(self, state: AgentState, action: str) -> str:
        if action == END:
            return "所有子任务已完成，本次协作结束。"
        return f"协调者将任务分派给 {action}。"

    def observe(self, state: AgentState, action: str, result: str) -> dict[str, Any]:
        return {
            "messages": [AIMessage(content=result, name=self.name)],
            "current_agent": self.name,
            "next_agent": action,
        }


class _WorkerNode(BaseAgentNode):
    """子 Agent 节点基类：处理任务后交还 Supervisor 并标记完成."""

    def think(self, state: AgentState) -> str:
        task = state.get("task_context")
        return f"解析任务：{task.description if task else ''}"

    def decide(self, state: AgentState, plan: str) -> str:
        return AgentRole.SUPERVISOR.value

    def execute(self, state: AgentState, action: str) -> str:
        return f"{self.name} 的回复（占位）：任务已处理完毕。"

    def observe(self, state: AgentState, action: str, result: str) -> dict[str, Any]:
        task = state.get("task_context")
        completed_task = (
            task.model_copy(update={"status": TaskStatus.COMPLETED})
            if task is not None
            else TaskContext(status=TaskStatus.COMPLETED)
        )
        return {
            "messages": [AIMessage(content=result, name=self.name)],
            "current_agent": self.name,
            "next_agent": action,
            "task_context": completed_task,
        }


class TeachingAssistantNode(_WorkerNode):
    """助教 Agent 节点（角色行为在阶段二 2.1 实现）."""

    def __init__(self) -> None:
        super().__init__(AgentRole.TEACHING_ASSISTANT)


class LearningAssistantNode(_WorkerNode):
    """助学 Agent 节点（角色行为在阶段二 2.1 实现）."""

    def __init__(self) -> None:
        super().__init__(AgentRole.LEARNING_ASSISTANT)


class EvaluatorNode(_WorkerNode):
    """评价 Agent 节点（角色行为在阶段二 2.1 实现）."""

    def __init__(self) -> None:
        super().__init__(AgentRole.EVALUATOR)