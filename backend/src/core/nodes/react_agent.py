"""所有角色共用的简易 ReAct Agent。"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from ..context import trim_message_history
from ..events import ErrorCode, EventType, RunError, RunEvent
from ..state import AgentRole, AgentState, ToolResult
from ..tools import ToolExecutor


@dataclass(slots=True)
class ReActResult:
    """一次 Agent 调用产生的状态更新与执行信息。"""

    updates: dict[str, Any] = field(default_factory=dict)
    messages: list[BaseMessage] = field(default_factory=list)
    error: RunError | None = None
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
        max_context_messages: int | None = None,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if max_context_messages is not None and max_context_messages < 3:
            raise ValueError("max_context_messages must be at least 3")
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.tool_executor = tool_executor or ToolExecutor()
        self.max_iterations = max_iterations
        self.max_context_messages = max_context_messages

    def run(self, state: AgentState) -> ReActResult:
        """运行到模型给出最终回答，或达到最大轮数。"""
        persisted_history = list(state.get("messages", []))
        if self.max_context_messages is None:
            history = list(persisted_history)
            context_trimmed = 0
        else:
            context_window = trim_message_history(
                persisted_history,
                self.max_context_messages,
            )
            history = list(context_window.messages)
            context_trimmed = context_window.trimmed_count
        extra = {
            **state.get("extra", {}),
            "context_trimmed": context_trimmed,
            "context_message_count": len(history),
        }
        generated: list[BaseMessage] = []
        tool_results: list[ToolResult] = []
        events: list[RunEvent] = []
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )
        session_id = state.get("session_id")
        started_at = perf_counter()

        def emit(event_type: EventType, **values: Any) -> None:
            nonlocal sequence
            sequence += 1
            events.append(
                RunEvent(
                    event_type=event_type,
                    sequence=sequence,
                    session_id=session_id,
                    agent=self.role.value,
                    **values,
                )
            )

        emit(EventType.AGENT_STARTED)

        for iteration in range(1, self.max_iterations + 1):
            messages = [
                SystemMessage(content=self.system_prompt),
                *history,
                *generated,
            ]
            try:
                response = self.model.invoke(messages)
            except Exception as exc:  # noqa: BLE001 - 模型边界统一返回结构化错误
                error = RunError(
                    error_code=ErrorCode.MODEL_CALL_FAILED,
                    message=f"模型调用失败：{exc!s}",
                    agent=self.role.value,
                )
                emit(
                    EventType.AGENT_COMPLETED,
                    success=False,
                    duration_ms=(perf_counter() - started_at) * 1000,
                    error_code=error.error_code,
                )
                return self._result(
                    generated,
                    tool_results,
                    events,
                    iteration,
                    extra,
                    error,
                )
            generated.append(response)

            if not response.tool_calls:
                emit(
                    EventType.AGENT_COMPLETED,
                    success=True,
                    duration_ms=(perf_counter() - started_at) * 1000,
                )
                return self._result(
                    generated,
                    tool_results,
                    events,
                    iteration,
                    extra,
                )

            # 工具返回的 ToolMessage 会进入下一轮模型输入，形成 Observation。
            for tool_call in response.tool_calls:
                emit(
                    EventType.TOOL_STARTED,
                    tool_name=str(tool_call.get("name", "")),
                )
                execution = self.tool_executor.execute(tool_call, self.role)
                generated.append(execution.message)
                tool_results.append(execution.result)
                emit(
                    EventType.TOOL_COMPLETED,
                    tool_name=execution.result.tool_name,
                    success=execution.result.success,
                    duration_ms=execution.result.duration_ms,
                    error_code=execution.result.error_code,
                )

        error = RunError(
            error_code=ErrorCode.REACT_ITERATION_LIMIT,
            message=f"ReAct 循环达到最大轮数：{self.max_iterations}",
            agent=self.role.value,
        )
        emit(
            EventType.AGENT_COMPLETED,
            success=False,
            duration_ms=(perf_counter() - started_at) * 1000,
            error_code=error.error_code,
        )
        return self._result(
            generated,
            tool_results,
            events,
            self.max_iterations,
            extra,
            error,
        )

    def _result(
        self,
        messages: list[BaseMessage],
        tool_results: list[ToolResult],
        events: list[RunEvent],
        iterations: int,
        extra: dict[str, Any],
        error: RunError | None = None,
    ) -> ReActResult:
        """统一整理写回 AgentState 的数据。"""
        updates: dict[str, Any] = {
            "current_agent": self.role.value,
            "messages": messages,
            "tool_results": tool_results,
            "events": events,
            "extra": extra,
        }
        return ReActResult(
            updates=updates,
            messages=messages,
            error=error,
            metadata={"iterations": iterations, "role": self.role.value},
        )
