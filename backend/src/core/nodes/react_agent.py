"""所有角色共用的简易 ReAct Agent。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from ..state import AgentRole, AgentState, ToolResult
from ..tools import ToolExecutor


@dataclass(slots=True)
class ReActResult:
    """一次 Agent 调用产生的状态更新与执行信息。"""

    updates: dict[str, Any] = field(default_factory=dict)
    messages: list[BaseMessage] = field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatModel(Protocol):
    """ReAct 循环需要的最小模型接口。"""

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        """根据消息生成回答或 Tool Call。"""
        ...


class ReActAgentNode:
    """执行“模型决策 → 工具执行 → 结果观察”的循环。"""

    def __init__(
        self,
        *,
        role: AgentRole,
        system_prompt: str,
        model: ChatModel,
        tool_executor: ToolExecutor | None = None,
        max_iterations: int = 5,
    ) -> None:
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.tool_executor = tool_executor or ToolExecutor()
        self.max_iterations = max_iterations

    def run(self, state: AgentState) -> ReActResult:
        """运行到模型给出最终回答，或达到最大轮数。"""
        history = list(state.get("messages", []))
        generated: list[BaseMessage] = []
        tool_results: list[ToolResult] = []

        for iteration in range(1, self.max_iterations + 1):
            messages = [
                SystemMessage(content=self.system_prompt),
                *history,
                *generated,
            ]
            try:
                response = self.model.invoke(messages)
            except Exception as exc:  # noqa: BLE001 - 模型边界统一返回结构化错误
                error = f"模型调用失败：{exc!s}"
                return self._result(generated, tool_results, iteration, error)
            generated.append(response)

            if not response.tool_calls:
                return self._result(generated, tool_results, iteration)

            # 工具返回的 ToolMessage 会进入下一轮模型输入，形成 Observation。
            for tool_call in response.tool_calls:
                execution = self.tool_executor.execute(tool_call, self.role)
                generated.append(execution.message)
                tool_results.append(execution.result)

        error = f"ReAct 循环达到最大轮数：{self.max_iterations}"
        return self._result(generated, tool_results, self.max_iterations, error)

    def _result(
        self,
        messages: list[BaseMessage],
        tool_results: list[ToolResult],
        iterations: int,
        error: str | None = None,
    ) -> ReActResult:
        """统一整理写回 AgentState 的数据。"""
        updates: dict[str, Any] = {
            "current_agent": self.role.value,
            "messages": messages,
            "tool_results": tool_results,
        }
        return ReActResult(
            updates=updates,
            messages=messages,
            error=error,
            metadata={"iterations": iterations, "role": self.role.value},
        )
