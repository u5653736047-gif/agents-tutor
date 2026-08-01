"""工具执行器测试。"""

from langchain_core.tools import tool
from pydantic import BaseModel, Field, field_validator

from core.events import ErrorCode
from core.state import AgentRole
from core.tools.executor import ToolExecutor
from core.tools.registry import ToolRegistry


@tool
def double(value: int) -> int:
    """返回输入数字的两倍。"""
    return value * 2


@tool
def broken_tool() -> str:
    """用于验证工具异常会变成 Observation。"""
    raise RuntimeError("工具不可用")


class PositiveValue(BaseModel):
    """工具内部使用的业务模型。"""

    value: int = Field(gt=0)


class ExplodingArguments(BaseModel):
    """模拟非 Pydantic 标准校验异常。"""

    value: int

    @field_validator("value")
    @classmethod
    def fail_schema_parsing(cls, value: int) -> int:
        raise RuntimeError("参数 Schema 解析失败")


@tool
def invalid_business_model(value: int) -> str:
    """模拟工具内部的 Pydantic 业务校验失败。"""
    return PositiveValue(value=-value).model_dump_json()


@tool(args_schema=ExplodingArguments)
def exploding_schema(value: int) -> int:
    """带自定义失败参数 Schema 的真实工具。"""
    return value


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
    assert execution.result.error_code is None


def test_tool_executor_classifies_unknown_tool() -> None:
    execution = ToolExecutor().execute(tool_call("missing"), AgentRole.EVALUATOR)

    assert execution.result.error_code is ErrorCode.TOOL_UNKNOWN
    assert execution.result.success is False
    assert "错误" in str(execution.message.content)


def test_tool_executor_classifies_unauthorized_tool() -> None:
    registry = ToolRegistry()
    registry.register(double, allowed_roles={AgentRole.EVALUATOR})
    executor = ToolExecutor(registry)

    execution = executor.execute(tool_call("double"), AgentRole.TEACHING_ASSISTANT)

    assert execution.result.error_code is ErrorCode.TOOL_UNAUTHORIZED
    assert execution.result.success is False
    assert "错误" in str(execution.message.content)


def test_tool_executor_classifies_invalid_arguments() -> None:
    executor = ToolExecutor([double])
    call = tool_call("double")
    call["args"] = {"wrong": 3}

    execution = executor.execute(call, AgentRole.TEACHING_ASSISTANT)

    assert execution.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
    assert execution.result.success is False
    assert "错误" in str(execution.message.content)


def test_tool_executor_classifies_runtime_failure() -> None:
    executor = ToolExecutor([broken_tool])

    execution = executor.execute(tool_call("broken_tool"), AgentRole.EVALUATOR)

    assert execution.result.error_code is ErrorCode.TOOL_EXECUTION_FAILED
    assert execution.result.success is False
    assert "工具不可用" in (execution.result.error or "")
    assert "错误" in str(execution.message.content)


def test_tool_executor_treats_internal_validation_error_as_runtime_failure() -> None:
    executor = ToolExecutor([invalid_business_model])
    call = tool_call("invalid_business_model")
    call["args"] = {"value": 1}

    execution = executor.execute(call, AgentRole.EVALUATOR)

    assert execution.result.error_code is ErrorCode.TOOL_EXECUTION_FAILED
    assert execution.result.success is False
    assert "错误" in str(execution.message.content)


def test_tool_executor_turns_schema_runtime_error_into_observation() -> None:
    executor = ToolExecutor([exploding_schema])
    call = tool_call("exploding_schema")
    call["args"] = {"value": 1}

    execution = executor.execute(call, AgentRole.EVALUATOR)

    assert execution.result.error_code is ErrorCode.TOOL_EXECUTION_FAILED
    assert execution.result.success is False
    assert "参数 Schema 解析失败" in (execution.result.error or "")
    assert "错误" in str(execution.message.content)
