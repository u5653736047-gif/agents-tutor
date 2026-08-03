"""LaTeX 公式渲染工具测试。"""

from __future__ import annotations

from core.events import ErrorCode
from core.state import AgentRole
from core.tools import ToolExecutor, create_render_formula_tool


def test_valid_inline_formula_returns_mathml() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"\frac{a}{b}"})

    assert result["valid"] is True
    assert "<math" in result["mathml"]
    assert 'display="inline"' in result["mathml"]
    assert "<mfrac>" in result["mathml"]


def test_block_display_flag_uses_block_mathml() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"E = mc^2", "display": True})

    assert result["valid"] is True
    assert 'display="block"' in result["mathml"]


def test_unbalanced_open_brace_is_reported() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"\frac{a"})

    assert result["valid"] is False
    assert "未闭合" in result["message"]


def test_stray_closing_brace_is_reported() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"a}"})

    assert result["valid"] is False
    assert "多余的右大括号" in result["message"]


def test_escaped_braces_do_not_count_toward_balance() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"\{x\}"})

    assert result["valid"] is True


def test_mismatched_begin_end_environments_are_reported() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"\begin{matrix}a&b\end{cases}"})

    assert result["valid"] is False
    assert "不匹配" in result["message"]


def test_unclosed_environment_is_reported() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": r"\begin{matrix}a&b"})

    assert result["valid"] is False
    assert "未闭合的环境" in result["message"]


def test_malformed_formula_is_structured_result_not_exception() -> None:
    tool = create_render_formula_tool()

    result = tool.invoke({"latex": "{"})

    assert result["valid"] is False
    assert "未闭合" in result["message"]


def test_blank_latex_is_rejected_as_invalid_arguments() -> None:
    tool = create_render_formula_tool()
    execution = ToolExecutor([tool]).execute(
        {"name": "render_formula", "args": {"latex": " "}, "id": "f-1"},
        AgentRole.TEACHING_ASSISTANT,
    )

    assert execution.result.success is False
    assert execution.result.error_code is ErrorCode.TOOL_INVALID_ARGUMENTS
