"""最小工具执行器测试。"""

from langchain_core.tools import tool

from core.state import AgentRole
from core.tools.executor import ToolExecutor


@tool
def double(value: int) -> int:
    """返回输入数字的两倍。"""
    return value * 2


@tool
def broken_tool() -> str:
    """用于验证工具异常会变成 Observation。"""
    raise RuntimeError("工具不可用")


def tool_call(name: str) -> dict[str, object]:
    args = {"value": 3} if name == "double" else {}
    return {"name": name, "args": args, "id": "call-1", "type": "tool_call"}


def test_tool_executor_records_successful_result() -> None:
    executor = ToolExecutor([double])

    execution = executor.execute(tool_call("double"), AgentRole.TEACHING_ASSISTANT)

    assert execution.message.content == "6"
    assert execution.result.success is True
    assert execution.result.output == "6"
    assert execution.result.tool_name == "double"


def test_tool_executor_turns_failures_into_observations() -> None:
    executor = ToolExecutor([broken_tool])

    unknown = executor.execute(tool_call("missing"), AgentRole.EVALUATOR)
    failed = executor.execute(tool_call("broken_tool"), AgentRole.EVALUATOR)

    assert unknown.result.success is False
    assert "未注册工具" in (unknown.result.error or "")
    assert failed.result.success is False
    assert "工具不可用" in (failed.result.error or "")
    assert "错误" in str(failed.message.content)
