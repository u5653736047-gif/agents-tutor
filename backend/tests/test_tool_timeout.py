"""工具执行超时控制测试。"""

from __future__ import annotations

import time

import pytest
from langchain_core.tools import tool

from core.events import ErrorCode
from core.state import AgentRole
from core.tools import ToolExecutor


@tool
def slow_tool(delay: float) -> str:
    """模拟耗时工具。"""
    time.sleep(delay)
    return "done"


@tool
def quick_tool() -> str:
    """模拟瞬时工具。"""
    return "instant"


def test_executor_default_timeout_turns_slow_tool_into_timeout_error() -> None:
    executor = ToolExecutor([slow_tool], default_timeout=0.3)

    execution = executor.execute(
        {"name": "slow_tool", "args": {"delay": 5.0}, "id": "slow-1"},
        AgentRole.TEACHING_ASSISTANT,
    )

    assert execution.result.success is False
    assert execution.result.error_code is ErrorCode.TOOL_TIMEOUT
    assert execution.result.error == "工具执行超时"
    assert "错误" in execution.message.content


def test_per_call_timeout_overrides_default() -> None:
    executor = ToolExecutor([slow_tool], default_timeout=60.0)

    execution = executor.execute(
        {"name": "slow_tool", "args": {"delay": 5.0}, "id": "slow-2"},
        AgentRole.TEACHING_ASSISTANT,
        timeout=0.2,
    )

    assert execution.result.success is False
    assert execution.result.error_code is ErrorCode.TOOL_TIMEOUT


def test_executor_without_timeout_runs_normal_tool() -> None:
    executor = ToolExecutor([quick_tool])

    execution = executor.execute(
        {"name": "quick_tool", "args": {}, "id": "quick-1"},
        AgentRole.TEACHING_ASSISTANT,
    )

    assert execution.result.success is True
    assert execution.result.output == "instant"


def test_invalid_default_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        ToolExecutor([quick_tool], default_timeout=0)
