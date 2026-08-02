"""把一次 Tool Call 转换为可供模型观察的结果。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, cast

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
}


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
    ) -> None:
        if isinstance(tools, ToolRegistry):
            if registry is not None:
                raise ValueError("不能重复指定工具注册表")
            registry = tools
        elif registry is None:
            registry = ToolRegistry(tools)
        elif tools:
            raise ValueError("不能同时指定工具列表和工具注册表")
        self.registry = registry

    def public_tool_name(self, tool_call: Mapping[str, Any]) -> str:
        """只公开注册表中的规范名称，避免模型生成名称进入持久状态。"""
        requested_name = str(tool_call.get("name") or "")
        tool = self.registry.get(requested_name)
        return tool.name if tool is not None else UNKNOWN_TOOL_NAME

    def execute(
        self,
        tool_call: Mapping[str, Any],
        agent_role: AgentRole,
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
                    output = _to_text(tool.invoke(dict(args)))
                    success = True
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


def _to_text(value: Any) -> str:
    """将工具输出稳定地转换为模型可读取的文本。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)
