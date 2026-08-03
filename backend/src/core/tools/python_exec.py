"""Python 代码执行沙箱工具。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, field_validator

_SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")


class _ExecutePythonInput(BaseModel):
    """校验工具输入，使错误可在调用前被分类。"""

    code: str = Field(min_length=1, max_length=4096)
    timeout: float = Field(default=10.0, ge=0.5, le=60.0)

    @field_validator("code")
    @classmethod
    def reject_blank_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code must not be empty")
        return value


def create_python_exec_tool(
    *,
    timeout: float = 10.0,
    max_output_chars: int = 8192,
) -> BaseTool:
    """创建隔离沙箱中的 Python 代码执行工具。

    沙箱边界：`python -I` 隔离模式（不加载 site-packages、忽略 PYTHONPATH、
    不把工作目录加入 sys.path），在独立临时目录中运行，按墙钟时间强杀，
    输出截断到上限。子进程环境剔除常见密钥类变量。
    """

    @tool("execute_python", args_schema=_ExecutePythonInput)
    def execute_python(code: str, timeout: float = timeout) -> dict[str, Any]:
        """在隔离沙箱中执行 Python 代码，返回 stdout/stderr 与退出码。"""
        workdir = Path(tempfile.mkdtemp(prefix="agent-python-"))
        try:
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", "-c", code],
                    cwd=workdir,
                    env=_scrubbed_env(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,  # 非零退出码属于可观察结果，不视为异常
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "status": "timeout",
                    "exit_code": None,
                    "stdout": _truncate(_as_text(exc.stdout), max_output_chars),
                    "stderr": _truncate(_as_text(exc.stderr), max_output_chars),
                }
            return {
                "status": "ok" if completed.returncode == 0 else "error",
                "exit_code": completed.returncode,
                "stdout": _truncate(completed.stdout, max_output_chars),
                "stderr": _truncate(completed.stderr, max_output_chars),
            }
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return execute_python


def _scrubbed_env() -> dict[str, str]:
    """剔除常见密钥类环境变量，避免学生代码读取宿主配置。"""
    markers = _SECRET_MARKERS
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in markers)
    }


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（输出超过 {limit} 字符，已截断）"


__all__ = ["create_python_exec_tool"]
