"""把一次 Tool Call 转换为可供模型观察的结果。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import dataclass
from math import isfinite
from time import perf_counter
from typing import Any, cast

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from ..events import ErrorCode
from ..state import AgentRole, ToolResult
from .office_tools import office_targets_within_roots
from .registry import ToolRegistry

UNKNOWN_TOOL_NAME = "unknown_tool"
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0  # 工具默认超时 30 秒
_TOOL_WORKER_COUNT = 4

# 错误码 → 中文提示的固定映射，模型只看到稳定文案，不暴露内部异常
_SAFE_ERRORS = {
    ErrorCode.TOOL_UNKNOWN: "未注册工具",
    ErrorCode.TOOL_UNAUTHORIZED: "当前角色无权调用该工具",
    ErrorCode.TOOL_INVALID_ARGUMENTS: "工具参数无效",
    ErrorCode.TOOL_EXECUTION_FAILED: "工具执行失败",
    ErrorCode.TOOL_TIMEOUT: "工具执行超时",
    ErrorCode.TOOL_NO_PROGRESS: "相同工具参数已执行过，请使用已有结果继续回答",
    ErrorCode.TOOL_BUDGET_EXCEEDED: "本轮工具调用预算已用尽，请基于已有结果回答",
    ErrorCode.TOOL_APPROVAL_REJECTED: "用户拒绝了该工具调用，请勿执行并继续安全回答",
    ErrorCode.TOOL_APPROVAL_QUEUE_LIMIT: "一次只能等待一个工具审批，请合并命令后重试",
}


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """工具执行后写回 ReAct 循环的两种结果。"""

    message: ToolMessage
    result: ToolResult


@dataclass(frozen=True, slots=True)
class PreparedToolApproval:
    """A schema-validated exact call safe to persist before approval."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


class ToolExecutor:
    """按名称执行 LangChain 工具，并统一记录成功或失败。"""

    def __init__(
        self,
        tools: Sequence[BaseTool] | ToolRegistry = (),
        *,
        registry: ToolRegistry | None = None,
        tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        tool_timeouts: Mapping[str, float] | None = None,
    ) -> None:
        # 三种入参方式统一成一份注册表：工具列表、现成注册表、或列表就地转注册表
        if isinstance(tools, ToolRegistry):
            if registry is not None:
                raise ValueError("不能重复指定工具注册表")
            registry = tools
        elif registry is None:
            registry = ToolRegistry(tools)  # 只给了列表，就地建注册表
        elif tools:
            raise ValueError("不能同时指定工具列表和工具注册表")
        self.registry = registry
        self._tool_timeout_seconds = _validate_timeout(
            tool_timeout_seconds,
            "tool_timeout_seconds",
        )
        configured_timeouts = dict(tool_timeouts or {})
        registered_names = {tool.name for tool in self.registry.list_tools()}
        unknown_names = set(configured_timeouts) - registered_names  # 配置里出现未注册工具名，多半是拼写错误
        if unknown_names:
            names = ", ".join(sorted(unknown_names))
            raise ValueError(f"tool_timeouts 包含未注册工具：{names}")
        self._tool_timeouts = {
            name: _validate_timeout(timeout, f"tool_timeouts[{name!r}]")
            for name, timeout in configured_timeouts.items()
        }
        self._pool = ThreadPoolExecutor(
            max_workers=_TOOL_WORKER_COUNT,  # 固定 4 个线程并行执行工具
            thread_name_prefix="tool-executor",
        )
        # 子代理本身会在执行期间再次调用本执行器中的业务工具。若两层
        # 共用一个池，并发子代理占满全部线程后会互相等待内部工具，形成
        # 线程饥饿。单独的池隔离父级等待与子级业务调用。
        self._subagent_pool = ThreadPoolExecutor(
            max_workers=_TOOL_WORKER_COUNT,
            thread_name_prefix="subagent-executor",
        )

    def timeout_seconds_for(self, tool_name: str) -> float:
        """返回指定工具的超时秒数，未覆盖时使用全局配置。"""
        return self._tool_timeouts.get(tool_name, self._tool_timeout_seconds)

    def public_tool_name(self, tool_call: Mapping[str, Any]) -> str:
        """只公开注册表中的规范名称，避免模型生成名称进入持久状态。"""
        requested_name = str(tool_call.get("name") or "")
        tool = self.registry.get(requested_name)  # 只认注册过的名字，模型伪造的名称统一归为 unknown
        return tool.name if tool is not None else UNKNOWN_TOOL_NAME

    def requires_approval(self, tool_call: Mapping[str, Any]) -> bool:
        """Return whether the registered tool is marked as approval-gated."""
        tool = self.registry.get(str(tool_call.get("name") or ""))
        extras = None if tool is None else getattr(tool, "extras", None)
        return isinstance(extras, Mapping) and extras.get("requires_approval") is True

    def artifact_auto_approval_root(
        self,
        tool_call: Mapping[str, Any],
        roots: Sequence[str],
    ) -> str | None:
        """工作流产物区自动授权判定（lesson-workflow-design §五）。

        仅 officecli_edit 参与豁免（shell 永不豁免：工作区授权不是系统级
        命令沙箱）；豁免条件是命令涉及的全部文件都落在产物根内。调用方
        （react_agent）须处于 workspace_scope 上下文——与执行路径同一前
        提。返回命中的产物根（执行时据此进入自动授权上下文），不豁免
        返回 None。roots 由调用方从 state.workflow 显式传入（检查发生在
        作用域建立之前，不走 ContextVar）。
        """
        tool = self.registry.get(str(tool_call.get("name") or ""))
        if tool is None or tool.name != "officecli_edit":
            return None
        if not roots:
            return None
        args = tool_call.get("args", {})
        command = args.get("command") if isinstance(args, Mapping) else None
        if not isinstance(command, list):
            return None
        tokens = [str(token) for token in command]
        if office_targets_within_roots(tokens, roots):
            return roots[0]
        return None

    def prepare_approval(
        self,
        tool_call: Mapping[str, Any],
        agent_role: AgentRole,
    ) -> PreparedToolApproval | ToolExecution:
        """Validate existence, authorization and schema without invoking a tool."""
        call_id = str(tool_call.get("id") or "unknown")
        requested_name = str(tool_call.get("name") or "")
        tool = self.registry.get(requested_name)
        args = tool_call.get("args", {})
        error_code: ErrorCode | None = None
        validated_arguments: dict[str, Any] | None = None
        if tool is None:
            error_code = ErrorCode.TOOL_UNKNOWN
        elif not self.registry.is_authorized(requested_name, agent_role):
            error_code = ErrorCode.TOOL_UNAUTHORIZED
        elif not isinstance(args, Mapping):
            error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
        else:
            try:
                input_schema = cast(type[BaseModel], tool.get_input_schema())
                validated = input_schema.model_validate(dict(args))
                validated_arguments = validated.model_dump(mode="python")
            except ValidationError:
                error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
            except Exception:  # noqa: BLE001 - schema boundary exposes stable errors
                error_code = ErrorCode.TOOL_EXECUTION_FAILED
        if error_code is not None:
            return self.reject(tool_call, agent_role, error_code)
        if tool is None or validated_arguments is None:
            raise RuntimeError("approval preparation produced no validated tool")
        return PreparedToolApproval(
            tool_call_id=call_id,
            tool_name=tool.name,
            arguments=validated_arguments,
        )

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
        started_at = perf_counter()  # 开始计时，用于输出耗时统计

        tool = self.registry.get(requested_name)
        success = False
        output = ""
        error_code: ErrorCode | None = None

        # 安检流水线：工具存在 → 角色授权 → 参数校验 → 线程池执行
        if tool is None:
            error_code = ErrorCode.TOOL_UNKNOWN
        elif not self.registry.is_authorized(requested_name, agent_role):  # 工具在，但角色无权调用
            error_code = ErrorCode.TOOL_UNAUTHORIZED
        elif not isinstance(args, Mapping):  # 参数连字典都不是，直接判无效
            error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
        else:
            try:
                input_schema = cast(type[BaseModel], tool.get_input_schema())
                input_schema.model_validate(dict(args))  # 按 pydantic schema 校验参数
            except ValidationError:  # 校验不过 → 参数无效，模型重新生成参数
                error_code = ErrorCode.TOOL_INVALID_ARGUMENTS
            except Exception:  # noqa: BLE001 - Schema 边界只公开稳定错误分类
                error_code = ErrorCode.TOOL_EXECUTION_FAILED
            else:
                try:
                    # 复制当前 ContextVar 上下文再进入工具线程。LangGraph 的
                    # stream writer 与子代理运行上下文都通过 ContextVar
                    # 传播；直接 submit 会在新线程丢失它们，导致工具内部
                    # 的 token/进度事件无法回到父运行。
                    runtime_context = copy_context()
                    extras = getattr(tool, "extras", None)
                    execution_pool = (
                        self._subagent_pool
                        if isinstance(extras, Mapping)
                        and extras.get("subagent") is True
                        else self._pool
                    )
                    future = execution_pool.submit(
                        runtime_context.run,
                        tool.invoke,
                        dict(args),
                    )
                    done, _ = wait(
                        {future},
                        timeout=self.timeout_seconds_for(tool.name),
                    )
                    if not done:  # 超过时限还没跑完，按超时处理
                        # 只能取消尚未开始的任务；运行中的线程会自行结束，不做危险强杀。
                        future.cancel()
                        error_code = ErrorCode.TOOL_TIMEOUT
                    else:
                        raw_output = future.result()
                        output = _to_text(raw_output)  # 成功：输出统一转文本
                        extras = getattr(tool, "extras", None)
                        reports_status = (
                            isinstance(extras, Mapping)
                            and extras.get("status_from_ok") is True
                        )
                        if (
                            reports_status
                            and isinstance(raw_output, Mapping)
                            and raw_output.get("ok") is False
                        ):
                            error_code = ErrorCode.TOOL_EXECUTION_FAILED
                        else:
                            success = True
                except Exception:  # noqa: BLE001 - 工具边界只公开稳定错误分类
                    error_code = ErrorCode.TOOL_EXECUTION_FAILED

        error = None if error_code is None else _SAFE_ERRORS[error_code]  # 只暴露稳定的错误分类提示
        duration_ms = (perf_counter() - started_at) * 1000  # 记录耗时，供系统侧统计
        # 某些工具（如 shell）会在结构化失败结果中携带有界 stdout/
        # stderr；此时把结果本身交给模型，避免只剩泛化错误而无法诊断。
        content = output if output else (output if success else f"错误：{error}")
        result = ToolResult(  # 结构化记录：留给系统分析用
            tool_call_id=call_id,
            tool_name=tool_name,
            agent_role=agent_role,
            success=success,
            output=output,
            error=error,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        message = ToolMessage(  # 文本消息：直接作为模型的观察结果
            content=content,
            tool_call_id=call_id,
            name=tool_name,
        )
        return ToolExecution(message=message, result=result)

    def reject(
        self,
        tool_call: Mapping[str, Any],
        agent_role: AgentRole,
        error_code: ErrorCode,
    ) -> ToolExecution:
        """Return a safe observation for a policy-blocked call without executing it."""
        if error_code not in _SAFE_ERRORS:
            raise ValueError("unsupported tool rejection error code")
        call_id = str(tool_call.get("id") or "unknown")
        tool_name = self.public_tool_name(tool_call)
        error = _SAFE_ERRORS[error_code]
        result = ToolResult(
            tool_call_id=call_id,
            tool_name=tool_name,
            agent_role=agent_role,
            success=False,
            output="",
            error=error,
            error_code=error_code,
            duration_ms=0.0,
        )
        return ToolExecution(
            message=ToolMessage(
                content=f"错误：{error}",
                tool_call_id=call_id,
                name=tool_name,
            ),
            result=result,
        )


def _validate_timeout(value: float, field_name: str) -> float:
    """超时必须是有限正数，避免立即超时或永久等待。"""
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return float(value)


def _to_text(value: Any) -> str:
    """将工具输出稳定地转换为模型可读取的文本。"""
    if isinstance(value, str):
        return value  # 文本原样返回，不额外加引号
    try:
        return json.dumps(value, ensure_ascii=False, default=str)  # 复杂对象转 JSON，中文不转义
    except TypeError:
        return str(value)  # 序列化失败兜底：直接转字符串
