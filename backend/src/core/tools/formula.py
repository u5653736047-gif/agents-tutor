"""LaTeX 公式校验与 MathML 转换工具。"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, tool
from latex2mathml.converter import convert
from pydantic import BaseModel, Field, field_validator

_ENV_TOKEN_RE = re.compile(r"\\(begin|end)\{([^}]*)\}")


class _RenderFormulaInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    latex: str = Field(min_length=1, max_length=500)
    display: bool = Field(default=False, description="True 渲染为块级公式")

    @field_validator("latex")
    @classmethod
    def reject_blank_latex(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("latex must not be empty")
        return value


def create_render_formula_tool() -> BaseTool:
    """创建 LaTeX 公式校验与渲染工具。

    返回结构化结果而非抛异常：公式语法错误是教学内容的一部分，
    模型需要能观察到失败原因并指导修正。真正的可视化渲染由前端
    KaTeX 完成，这里负责语法校验与 MathML 转换。
    """

    @tool("render_formula", args_schema=_RenderFormulaInput)
    def render_formula(latex: str, display: bool = False) -> dict[str, Any]:
        """校验 LaTeX 公式并转换为 MathML。"""
        issue = _check_balanced_braces(latex) or _check_environments(latex)
        if issue is not None:
            return {"valid": False, "message": issue}
        try:
            mathml = convert(latex, display="block" if display else "inline")
        except Exception as exc:  # noqa: BLE001 - 语法错误属于可观察结果
            return {"valid": False, "message": f"公式语法错误：{exc}"}
        return {"valid": True, "mathml": mathml}

    return render_formula


def _check_balanced_braces(latex: str) -> str | None:
    """检查大括号配对（忽略转义），返回错误信息；配对正常返回 None。"""
    depth = 0
    escaped = False
    for index, char in enumerate(latex):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return f"第 {index} 个字符附近存在多余的右大括号 }}"
    if depth > 0:
        return f"存在 {depth} 个未闭合的左大括号 {{"
    return None


def _check_environments(latex: str) -> str | None:
    """按出现顺序检查 \\begin / \\end 环境配对。"""
    stack: list[str] = []
    for keyword, name in _ENV_TOKEN_RE.findall(latex):
        if keyword == "begin":
            stack.append(name)
        else:
            if not stack:
                return f"多余的 \\end{{{name}}}"
            if stack[-1] != name:
                return f"\\end{{{name}}} 与 \\begin{{{stack[-1]}}} 不匹配"
            stack.pop()
    if stack:
        return f"存在未闭合的环境 \\begin{{{stack[-1]}}}"
    return None


__all__ = ["create_render_formula_tool"]
