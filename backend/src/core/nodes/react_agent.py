"""所有角色共用的简易 ReAct Agent。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.config import get_stream_writer

from ..context import (
    MessageTokenCounter,
    trim_message_history,
)
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


def _noop_stream_writer(_: object) -> None:
    """Direct node calls have no LangGraph runtime and therefore no stream sink."""


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
        max_context_tokens: int | None = None,
        context_token_counter: MessageTokenCounter | None = None,
        # S2-T2 分层讲解：可选的「按状态动态构建系统提示词」钩子。
        # 为 None 时用构造时固定的 system_prompt（默认行为，既有单元
        # 测试契约不变）；提供时每个 ReAct 轮次都会用当前 state 重新
        # 生成提示词，让 learning_assistant 能按 state["level"] 切换
        # 讲解深度（见 factory.py 与 prompts.learning_assistant_system_prompt）。
        prompt_builder: Callable[[AgentState], str] | None = None,
    ) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        if max_context_messages is not None and max_context_messages < 3:
            raise ValueError("max_context_messages must be at least 3")
        if max_context_tokens is not None and max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.tool_executor = tool_executor or ToolExecutor()
        self.max_iterations = max_iterations
        self.max_context_messages = max_context_messages
        self.max_context_tokens = max_context_tokens
        self.context_token_counter = context_token_counter
        self.prompt_builder = prompt_builder

    def run(self, state: AgentState) -> ReActResult:
        """运行到模型给出最终回答，或达到最大轮数。"""
        # 先快照会话起点；本轮新增的消息/事件暂存于本地变量，结束时统一写回状态。
        persisted_history = list(state.get("messages", []))
        extra = dict(state.get("extra", {}))
        generated: list[BaseMessage] = []
        tool_results: list[ToolResult] = []
        events: list[RunEvent] = []
        sequence = max(  # 从历史事件续接序号，保证全会话事件递增
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )
        session_id = state.get("session_id")
        started_at = perf_counter()
        # graph.stream(custom) 场景下 writer 会立即送出事件；节点被直接
        # 调用（单测/独立复用）时没有 LangGraph runtime，安全退化为空写入器。
        try:
            stream_writer = get_stream_writer()
        except RuntimeError:
            stream_writer = _noop_stream_writer
        # S2-T2 分层讲解：有 prompt_builder 时按当前状态动态生成系统
        # 提示词（如 learning_assistant 按 state["level"] 分层讲解），
        # 否则沿用构造时固定的 system_prompt——默认行为与改动前完全一致。
        system_prompt = (
            self.system_prompt
            if self.prompt_builder is None
            else self.prompt_builder(state)
        )
        system_message = SystemMessage(content=system_prompt)

        def model_context() -> list[BaseMessage]:
            # 组装本轮模型上下文：会话历史 + 本轮新增，超预算时裁剪，统计信息写进 extra。
            combined = [*persisted_history, *generated]
            if (
                self.max_context_messages is None
                and self.max_context_tokens is None
            ):
                history = combined
                trimmed_count = 0
                token_count = None
            else:
                # 预算覆盖完整消息视图；绑定工具 Schema 不属于历史窗口。
                context_window = trim_message_history(
                    combined,
                    self.max_context_messages,
                    max_tokens=self.max_context_tokens,
                    token_counter=self.context_token_counter,
                    prefix_messages=(system_message,),
                )
                history = list(context_window.messages)
                trimmed_count = context_window.trimmed_count
                token_count = context_window.token_count
            extra["context_trimmed"] = trimmed_count
            extra["context_message_count"] = len(history)
            if token_count is None:
                extra.pop("context_token_count", None)
            else:
                extra["context_token_count"] = token_count
            return [system_message, *history]

        def emit(event_type: EventType, **values: Any) -> None:
            # 小工具：生成带自增序号的事件暂存，节点结束时统一写回状态。
            nonlocal sequence
            sequence += 1
            event = RunEvent(
                event_type=event_type,
                sequence=sequence,
                session_id=session_id,
                agent=self.role.value,
                **values,
            )
            events.append(event)
            stream_writer(
                {
                    "kind": "run_event",
                    "event": event.model_dump(mode="json"),
                }
            )

        emit(EventType.AGENT_STARTED)

        # ReAct 主循环：模型决策 → 工具执行 → 结果观察，直到模型直接回答或用尽轮数。
        for iteration in range(1, self.max_iterations + 1):
            messages = model_context()
            try:
                response = self.model.invoke(messages)  # 模型决策：可能给最终回答，也可能请求调用工具
            except Exception:  # noqa: BLE001 - 模型边界只公开稳定错误分类
                error = RunError(
                    error_code=ErrorCode.MODEL_CALL_FAILED,
                    message="模型调用失败",
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
            if not response.tool_calls:  # 没有工具请求 → 模型已给出最终回答，本轮结束
                generated.append(response)
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

            # 脱敏：写回历史前把内部工具名换成对外名称，避免内部命名泄漏给下游。
            public_tool_calls = [
                (self.tool_executor.public_tool_name(tool_call), tool_call)
                for tool_call in response.tool_calls
            ]
            additional_kwargs = dict(response.additional_kwargs)
            additional_kwargs.pop("tool_calls", None)
            public_content = (
                [
                    {
                        **block,
                        "name": self.tool_executor.public_tool_name(block),
                    }
                    if isinstance(block, Mapping)
                    and block.get("type") == "tool_call"
                    else block
                    for block in response.content
                ]
                if isinstance(response.content, list)
                else response.content
            )
            generated.append(
                response.model_copy(
                    update={
                        "additional_kwargs": additional_kwargs,
                        "content": public_content,
                        "tool_calls": [
                            {**tool_call, "name": public_name}
                            for public_name, tool_call in public_tool_calls
                        ],
                    }
                )
            )

            # 工具返回的 ToolMessage 会进入下一轮模型输入，形成 Observation。
            for public_name, tool_call in public_tool_calls:  # 逐个执行，异常已包装成失败结果
                emit(
                    EventType.TOOL_STARTED,
                    tool_name=public_name,
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

        # 兜底：轮数用尽仍未给出最终回答，按迭代上限错误结束。
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
