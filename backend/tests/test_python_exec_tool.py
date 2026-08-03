"""Python 代码执行沙箱工具测试。"""

from __future__ import annotations

import os

from core.events import ErrorCode
from core.state import AgentRole
from core.tools import ToolExecutor, create_python_exec_tool


def test_executes_code_and_returns_stdout() -> None:
    tool = create_python_exec_tool()

    result = tool.invoke({"code": "print('hello', 1 + 1)"})

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello 2"
    assert result["stderr"] == ""


def test_runtime_error_is_observable_result_not_exception() -> None:
    tool = create_python_exec_tool()

    result = tool.invoke({"code": "raise ValueError('boom')"})

    assert result["status"] == "error"
    assert result["exit_code"] != 0
    assert "ValueError" in result["stderr"]
    assert result["stdout"] == ""


def test_hanging_code_is_killed_by_timeout() -> None:
    tool = create_python_exec_tool(timeout=10.0)

    result = tool.invoke({"code": "import time; time.sleep(30)", "timeout": 0.5})

    assert result["status"] == "timeout"
    assert result["exit_code"] is None


def test_output_is_truncated_to_configured_limit() -> None:
    tool = create_python_exec_tool(max_output_chars=200)

    result = tool.invoke({"code": "print('x' * 5000)"})

    assert result["status"] == "ok"
    assert len(result["stdout"]) <= 200 + 40  # 截断标记的余量
    assert "已截断" in result["stdout"]


def test_third_party_imports_are_unavailable() -> None:
    tool = create_python_exec_tool()

    result = tool.invoke({"code": "import numpy"})

    assert result["status"] == "error"
    assert "No module named 'numpy'" in result["stderr"]


def test_secret_like_environment_variables_are_scrubbed() -> None:
    marker = "__TEST_API_KEY__"
    os.environ[marker] = "super-secret"
    try:
        tool = create_python_exec_tool()
        result = tool.invoke(
            {"code": f"import os; print({marker!r} in os.environ)"}
        )
    finally:
        del os.environ[marker]

    assert result["status"] == "ok"
    assert result["stdout"].strip() == "False"


def test_blank_code_is_rejected_as_invalid_arguments() -> None:
    tool = create_python_exec_tool()
    execution = ToolExecutor([tool]).execute(
        {"name": "execute_python", "args": {"code": "   "}, "id": "py-1"},
        AgentRole.TEACHING_ASSISTANT,
    )

    assert execution.result.success is False
    assert execution.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
