"""把一次 Tool Call 转换为可供模型观察的结果。"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, TypeVar, cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from ..events import ErrorCode
from ..state import AgentRole, ToolResult
from .registry import ToolRegistry

UNKNOWN_TOOL_NAME = "unknown_tool"

_SAFE_ERRORS = {
    ErrorCode.TOOL_UNKNOWN: "未注册工具",
    ErrorCode.TOOL_UNAUTHORIZED: "当前角色无权调用该工具",
    ErrorCode.TOOL_INVALID_ARGUMENTS: "工具参数无效",
    ErrorCode.TOOL_EXECUTION_FAILED: "工具执行失败",
    ErrorCode.TOOL_TIMEOUT: "工具执行超时",
}

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """工具执行后写回 ReAct 循环的两种结果。"""

    message: ToolMessage
    result: ToolResult


class ToolExecutor:
    """按名称执行 LangChain 工具，并统一记录成功或失败。"""

    def __init__(
        self,
        tools: Sequence[BaseTool] | ToolRegistry = (),
        *,
        registry: ToolRegistry | None = None,
        default_timeout: float | None = None,
    ) -> None:
        if isinstance(tools, ToolRegistry):
            if registry is not None:
                raise ValueError("不能重复指定工具注册表")
            registry = tools
        elif registry is None:
            registry = ToolRegistry(tools)
        elif tools:
            raise ValueError("不能同时指定工具列表和工具注册表")
        if default_timeout is not None and default_timeout <= 0:
            raise ValueError("default_timeout must be positive")
        self.registry = registry
        self.default_timeout = default_timeout

    def public_tool_name(self, tool_call: Mapping[str, Any]) -> str:
        """只公开注册表中的规范名称，避免模型生成名称进入持久状态。"""
        requested_name = str(tool_call.get("name") or "")
        tool = self.registry.get(requested_name)
        return tool.name if tool is not None else UNKNOWN_TOOL_NAME

    def execute(
        self,
        tool_call: Mapping[str, Any],
        agent_role: AgentRole,
        *,
        timeout: float | None = None,
    ) -> ToolExecution:
        """执行工具；异常会成为 Observation，而不是打断 Agent。"""
        call_id = str(tool_call.get("id") or "unknown")
        requested_name = str(tool_call.get("name") or "")
        tool_name = self.public_tool_name(tool_call)
        args = tool_call.get("args", {})
        started_at = perf_counter()

        tool = self.registry.get(requested_name)
        success = False
        output = ""
        error_code: ErrorCode | None = None

        if tool is None:
            error_code = ErrorCode.TOOL_UNKNOWN
        elif not self.registry.is_authorized(requested_name, agent_role):
            error_code = ErrorCode.TOOL_UNAUTHORIZED
        elif not isinstance(args, Mapping):
            error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
        else:
            try:
                input_schema = cast(type[BaseModel], tool.get_input_schema())
                input_schema.model_validate(dict(args))
            except ValidationError:
                error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
            except Exception:  # noqa: BLE001 - Schema 边界只公开稳定错误分类
                error_code = ErrorCode.TOOL_EXECUTION_FAILED
            else:
                try:
                    effective_timeout = (
                        self.default_timeout if timeout is None else timeout
                    )
                    output = _to_text(
                        _invoke_with_timeout(
                            lambda: tool.invoke(dict(args)),
                            effective_timeout,
                        )
                    )
                    success = True
                except TimeoutError:
                    error_code = ErrorCode.TOOL_TIMEOUT
                except Exception:  # noqa: BLE001 - 工具边界只公开稳定错误分类
                    error_code = ErrorCode.TOOL_EXECUTION_FAILED

        error = None if error_code is None else _SAFE_ERRORS[error_code]
        duration_ms = (perf_counter() - started_at) * 1000
        content = output if success else f"错误：{error}"
        result = ToolResult(
            tool_call_id=call_id,
            tool_name=tool_name,
            agent_role=agent_role,
            success=success,
            output=output,
            error=error,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        message = ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=tool_name,
        )
        return ToolExecution(message=message, result=result)


def _invoke_with_timeout(
    fn: Callable[[], _T],
    timeout: float | None,
) -> _T:
    """在独立守护线程中执行工具，超时则抛出 TimeoutError。

    每次调用使用新线程，避免挂起的同步工具占用共享线程池；
    守护线程保证超时工具即使永远不返回，也不会阻塞进程退出。
    超时后线程仍会继续运行直到工具自行返回，这是同步调用的固有局限。
    """
    if timeout is None:
        return fn()

    result: list[tuple[bool, _T | BaseException]] = []

    def run() -> None:
        try:
            result.append((True, fn()))
        except BaseException as exc:  # noqa: BLE001 - 原样回传工具异常
            result.append((False, exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"工具执行超过 {timeout} 秒")
    success, value = result[0]
    if success:
        return cast(_T, value)
    raise cast(BaseException, value)


def _to_text(value: Any) -> str:
    """将工具输出稳定地转换为模型可读取的文本。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)
