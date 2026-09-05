"""所有角色共用的简易 ReAct Agent。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from contextvars import ContextVar
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
from ..filesystem import workspace_scope
from ..state import (
    WORKFLOW_ITERATION_HARD_CAP,
    AgentRole,
    AgentState,
    ToolApprovalRequest,
    ToolResult,
    WorkflowState,
    WorkflowStatus,
)
from ..tools import PreparedToolApproval, ToolExecution, ToolExecutor
from ..tools.artifact_scope import artifact_auto_approval

_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")
_REASONING_BLOCK_TYPES = {
    "reasoning",
    "reasoning_content",
    "reasoning_delta",
    "thinking",
    "thinking_delta",
}
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)
_REASONING_LIMIT = 8_000
_TOOL_INPUT_LIMIT = 4_000
_TOOL_OUTPUT_LIMIT = 8_000
_TRUNCATION_MARKER = "…[truncated]"
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|password|secret|token)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)
_ACTIVE_PARENT_TOOL_CALL_ID: ContextVar[str | None] = ContextVar(
    "active_parent_tool_call_id",
    default=None,
)


def _truncate_display(text: str, limit: int) -> str:
    """把过程正文限制在固定长度，避免 checkpoint/UI 被大结果撑爆。"""
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - len(_TRUNCATION_MARKER))]}{_TRUNCATION_MARKER}"


def _is_secret_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _redact_value(value: object) -> object:
    """递归脱敏结构化工具数据，同时保留普通参数供 UI 检查。"""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_secret_key(key) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _json_summary(value: object, limit: int) -> str:
    return _truncate_display(
        json.dumps(
            _redact_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
        limit,
    )


def _tool_input_summary(value: object) -> str:
    return _json_summary(value, _TOOL_INPUT_LIMIT)


def tool_output_summary(value: str) -> str | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        redacted = _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
            value,
        )
        return _truncate_display(redacted, _TOOL_OUTPUT_LIMIT)
    return _json_summary(parsed, _TOOL_OUTPUT_LIMIT)


def _reasoning_content(message: AIMessage) -> str | None:
    """读取 provider 显式 reasoning 字段；缺失时不合成、不伪造。"""
    for key in _REASONING_KEYS:
        value = message.additional_kwargs.get(key)
        if isinstance(value, str) and value:
            return _truncate_display(value, _REASONING_LIMIT)

    if not isinstance(message.content, list):
        return None
    parts: list[str] = []
    for block in message.content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") not in _REASONING_BLOCK_TYPES:
            continue
        for key in ("text", "reasoning_content", "reasoning", "thinking"):
            value = block.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    if not parts:
        return None
    return _truncate_display("".join(parts), _REASONING_LIMIT)


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
        max_tool_calls: int = 20,
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
        if max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")
        if max_context_messages is not None and max_context_messages < 3:
            raise ValueError("max_context_messages must be at least 3")
        if max_context_tokens is not None and max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.tool_executor = tool_executor or ToolExecutor()
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
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
        seen_tool_calls: set[tuple[str, str]] = set()
        tool_call_count = 0
        pending_tool_approval: ToolApprovalRequest | None = None
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

        # 工作流产物根（lesson-workflow-design §五）：仅工作流运行且登记
        # 了产物目录时参与自动授权；解析失败视为无产物区（宽容读取，
        # checkpoint 反序列化可能是 dict）。
        auto_approval_roots: tuple[str, ...] = ()
        raw_workflow = state.get("workflow")
        if raw_workflow is not None:
            try:
                workflow_model = (
                    raw_workflow
                    if isinstance(raw_workflow, WorkflowState)
                    else WorkflowState.model_validate(raw_workflow)
                )
            except Exception:  # noqa: BLE001 - 脏工作流状态不阻断工具执行
                workflow_model = None
            if (
                workflow_model is not None
                and workflow_model.status is WorkflowStatus.RUNNING
                and workflow_model.artifact_root
            ):
                auto_approval_roots = (workflow_model.artifact_root,)

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
            values.setdefault(
                "parent_tool_call_id",
                _ACTIVE_PARENT_TOOL_CALL_ID.get(),
            )
            event = RunEvent(
                event_type=event_type,
                sequence=sequence,
                session_id=session_id,
                run_id=state.get("run_id"),
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

        # 工作流步骤级迭代预算（lesson-workflow-design §四）：调度节点
        # 分派每步前写入 iteration_budget；None = 用构造默认。预算在
        # [1, WORKFLOW_ITERATION_HARD_CAP] 截断，异常值按默认处理——
        # 预算只由我方调度代码写入，此处防御不为模型开口子。
        iteration_budget = self.max_iterations
        raw_budget = state.get("iteration_budget")
        if isinstance(raw_budget, int) and not isinstance(raw_budget, bool):
            iteration_budget = max(1, min(raw_budget, WORKFLOW_ITERATION_HARD_CAP))

        # ReAct 主循环：模型决策 → 工具执行 → 结果观察，直到模型直接回答或用尽轮数。
        for iteration in range(1, iteration_budget + 1):
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
            reasoning = _reasoning_content(response)
            if reasoning is not None:
                emit(
                    EventType.AGENT_REASONING,
                    content=reasoning,
                    message_id=(response.id if isinstance(response.id, str) else None),
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
                tool_call_id = tool_call.get("id")
                emit(
                    EventType.TOOL_STARTED,
                    tool_name=public_name,
                    tool_call_id=(
                        str(tool_call_id) if tool_call_id is not None else None
                    ),
                    input_summary=_tool_input_summary(tool_call.get("args", {})),
                )
                parent_token = _ACTIVE_PARENT_TOOL_CALL_ID.set(
                    str(tool_call_id) if tool_call_id is not None else None
                )
                execution: ToolExecution | None = None
                auto_approved_call = False
                try:
                    fingerprint = (
                        public_name,
                        json.dumps(
                            tool_call.get("args", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                    )
                    if tool_call_count >= self.max_tool_calls:
                        execution = self.tool_executor.reject(
                            tool_call,
                            self.role,
                            ErrorCode.TOOL_BUDGET_EXCEEDED,
                        )
                    elif fingerprint in seen_tool_calls:
                        tool_call_count += 1
                        execution = self.tool_executor.reject(
                            tool_call,
                            self.role,
                            ErrorCode.TOOL_NO_PROGRESS,
                        )
                    else:
                        tool_call_count += 1
                        seen_tool_calls.add(fingerprint)
                        # 产物区自动授权判定（lesson-workflow-design §五）：
                        # 仅 officecli_edit 且命令全部落在工作流产物根内时
                        # 免人工审批（shell 等其余门控工具不参与）。判定
                        # 需要工作区上下文解析相对路径，与执行同一前提。
                        needs_approval = self.tool_executor.requires_approval(
                            tool_call
                        )
                        auto_root: str | None = None
                        if needs_approval and auto_approval_roots:
                            with workspace_scope(
                                state.get("workspace_root"),
                                additional_roots=state.get(
                                    "additional_workspace_roots",
                                    [],
                                ),
                            ):
                                auto_root = (
                                    self.tool_executor.artifact_auto_approval_root(
                                        tool_call,
                                        auto_approval_roots,
                                    )
                                )
                        if auto_root is not None:
                            with (
                                workspace_scope(
                                    state.get("workspace_root"),
                                    additional_roots=state.get(
                                        "additional_workspace_roots",
                                        [],
                                    ),
                                ),
                                artifact_auto_approval((auto_root,)),
                            ):
                                execution = self.tool_executor.execute(
                                    tool_call,
                                    self.role,
                                )
                            auto_approved_call = True
                        elif needs_approval:
                            prepared = self.tool_executor.prepare_approval(
                                tool_call,
                                self.role,
                            )
                            if isinstance(prepared, PreparedToolApproval):
                                if pending_tool_approval is None:
                                    pending_tool_approval = ToolApprovalRequest(
                                        tool_call_id=prepared.tool_call_id,
                                        tool_name=prepared.tool_name,
                                        agent_role=self.role,
                                        arguments=prepared.arguments,
                                    )
                                else:
                                    execution = self.tool_executor.reject(
                                        tool_call,
                                        self.role,
                                        ErrorCode.TOOL_APPROVAL_QUEUE_LIMIT,
                                    )
                            else:
                                execution = prepared
                        else:
                            with workspace_scope(
                                state.get("workspace_root"),
                                additional_roots=state.get(
                                    "additional_workspace_roots",
                                    [],
                                ),
                            ):
                                execution = self.tool_executor.execute(tool_call, self.role)
                finally:
                    _ACTIVE_PARENT_TOOL_CALL_ID.reset(parent_token)
                if execution is None:
                    # Exact call is now persisted and routed to an approval gate.
                    # Its ToolMessage is appended only after the gate is resumed.
                    continue
                generated.append(execution.message)
                tool_results.append(execution.result)
                emit(
                    EventType.TOOL_COMPLETED,
                    tool_name=execution.result.tool_name,
                    tool_call_id=execution.result.tool_call_id,
                    output_summary=tool_output_summary(execution.result.output),
                    success=execution.result.success,
                    duration_ms=execution.result.duration_ms,
                    error_code=execution.result.error_code,
                    auto_approved=True if auto_approved_call else None,
                )

            if pending_tool_approval is not None:
                return self._result(
                    generated,
                    tool_results,
                    events,
                    iteration,
                    extra,
                    pending_tool_approval=pending_tool_approval,
                )

        # 兜底：轮数用尽仍未给出最终回答，按迭代上限错误结束。
        error = RunError(
            error_code=ErrorCode.REACT_ITERATION_LIMIT,
            message=f"ReAct 循环达到最大轮数：{iteration_budget}",
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
            iteration_budget,
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
        pending_tool_approval: ToolApprovalRequest | None = None,
    ) -> ReActResult:
        """统一整理写回 AgentState 的数据。"""
        updates: dict[str, Any] = {
            "current_agent": self.role.value,
            "messages": messages,
            "tool_results": tool_results,
            "events": events,
            "extra": extra,
        }
        if pending_tool_approval is not None:
            updates["pending_tool_approval"] = pending_tool_approval
        return ReActResult(
            updates=updates,
            messages=messages,
            error=error,
            metadata={
                "iterations": iterations,
                "role": self.role.value,
                "paused_for_tool_approval": pending_tool_approval is not None,
            },
        )
