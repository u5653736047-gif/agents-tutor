"""LangChain wrappers for workspace-scoped, read-only filesystem operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..filesystem import (
    WorkspaceFileError,
    WorkspaceFileSystem,
    active_workspace_filesystem,
)

_READ_ONLY_EXTRAS: dict[str, object] = {
    "category": "filesystem",
    "read_only": True,
}
_INSPECT_WORKERS = 4
_INSPECT_MAX_OUTPUT_BYTES = 128 * 1024


class _StrictToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _WorkspaceInfoInput(_StrictToolInput):
    pass


class _ListFilesInput(_StrictToolInput):
    path: str = Field(default=".", min_length=1, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=100)

    @field_validator("path")
    @classmethod
    def reject_blank_path(cls, value: str) -> str:
        return _require_non_blank(value, "path")


class _GlobFilesInput(_StrictToolInput):
    pattern: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", min_length=1, max_length=4096)
    max_results: int = Field(default=100, ge=1, le=100)

    @field_validator("pattern", "path")
    @classmethod
    def reject_blank_values(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "value")
        return _require_non_blank(value, str(field_name))


class _GrepFilesInput(_StrictToolInput):
    query: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", min_length=1, max_length=4096)
    file_pattern: str = Field(default="**/*", min_length=1, max_length=500)
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=100)

    @field_validator("query", "path", "file_pattern")
    @classmethod
    def reject_blank_values(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "value")
        return _require_non_blank(value, str(field_name))


class _ReadFileInput(_StrictToolInput):
    path: str = Field(min_length=1, max_length=4096)
    line_offset: int = Field(default=1, ge=1)
    n_lines: int = Field(default=200, ge=1, le=1000)

    @field_validator("path")
    @classmethod
    def reject_blank_path(cls, value: str) -> str:
        return _require_non_blank(value, "path")


class _InspectOperation(_StrictToolInput):
    operation: Literal["list", "glob", "grep", "read"]
    path: str = Field(default=".", min_length=1, max_length=4096)
    pattern: str | None = Field(default=None, min_length=1, max_length=500)
    query: str | None = Field(default=None, min_length=1, max_length=500)
    file_pattern: str = Field(default="**/*", min_length=1, max_length=500)
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=100)
    line_offset: int = Field(default=1, ge=1)
    n_lines: int = Field(default=200, ge=1, le=1000)

    @model_validator(mode="after")
    def require_operation_arguments(self) -> _InspectOperation:
        if self.operation == "glob" and self.pattern is None:
            raise ValueError("glob operation requires pattern")
        if self.operation == "grep" and self.query is None:
            raise ValueError("grep operation requires query")
        return self


class _InspectWorkspaceInput(_StrictToolInput):
    operations: list[_InspectOperation] = Field(min_length=1, max_length=12)


def create_read_only_file_tools(
    filesystem: WorkspaceFileSystem,
) -> tuple[BaseTool, ...]:
    """Create tools bound to one filesystem capability instead of global disk access."""

    def current() -> WorkspaceFileSystem:
        return active_workspace_filesystem(filesystem)

    @tool(
        "workspace_info",
        args_schema=_WorkspaceInfoInput,
        extras=dict(_READ_ONLY_EXTRAS),
    )
    def workspace_info() -> dict[str, object]:
        """获取当前受限文件工作区的安全信息；询问工作区是什么或在哪里时必须先调用。"""
        return current().workspace_info()

    @tool(
        "list_files",
        args_schema=_ListFilesInput,
        extras=dict(_READ_ONLY_EXTRAS),
    )
    def list_files(path: str = ".", max_results: int = 100) -> dict[str, object]:
        """列出授权工作区目录的直接子项；路径可用相对或已授权绝对路径。"""
        return _recover_file_error(lambda: current().list_files(path, max_results=max_results))

    @tool(
        "glob_files",
        args_schema=_GlobFilesInput,
        extras=dict(_READ_ONLY_EXTRAS),
    )
    def glob_files(
        pattern: str,
        path: str = ".",
        max_results: int = 100,
    ) -> dict[str, object]:
        """按 Glob 模式查找工作区文本文件；结果会过滤敏感和忽略目录。"""
        return _recover_file_error(
            lambda: current().glob_files(
                pattern,
                path=path,
                max_results=max_results,
            )
        )

    @tool(
        "grep_files",
        args_schema=_GrepFilesInput,
        extras=dict(_READ_ONLY_EXTRAS),
    )
    def grep_files(
        query: str,
        path: str = ".",
        file_pattern: str = "**/*",
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> dict[str, object]:
        """在工作区 UTF-8 文本文件中搜索字面文本，不执行正则表达式。"""
        return _recover_file_error(
            lambda: current().grep_files(
                query,
                path=path,
                file_pattern=file_pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
        )

    @tool(
        "read_file",
        args_schema=_ReadFileInput,
        extras=dict(_READ_ONLY_EXTRAS),
    )
    def read_file(
        path: str,
        line_offset: int = 1,
        n_lines: int = 200,
    ) -> dict[str, object]:
        """分页读取工作区 UTF-8 文本文件；敏感、二进制及过大文件会被拒绝。"""
        return _recover_file_error(
            lambda: current().read_file(
                path,
                line_offset=line_offset,
                n_lines=n_lines,
            )
        )

    @tool(
        "inspect_workspace",
        args_schema=_InspectWorkspaceInput,
        extras=dict(_READ_ONLY_EXTRAS),
    )
    def inspect_workspace(
        operations: list[_InspectOperation],
    ) -> dict[str, object]:
        """一次并行执行多项只读 list/glob/grep/read，用于项目结构与代码分析。"""
        active = current()
        with ThreadPoolExecutor(
            max_workers=min(_INSPECT_WORKERS, len(operations)),
            thread_name_prefix="workspace-inspect",
        ) as pool:
            futures = [
                pool.submit(_run_inspect_operation, active, operation)
                for operation in operations
            ]
            completed = [future.result() for future in futures]

        results: list[dict[str, object]] = []
        used_bytes = 0
        truncated = False
        for operation, result in zip(operations, completed, strict=True):
            item: dict[str, object] = {
                "operation": operation.operation,
                "path": operation.path,
                "result": result,
            }
            item_bytes = len(
                json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")
            )
            if used_bytes + item_bytes > _INSPECT_MAX_OUTPUT_BYTES:
                truncated = True
                break
            results.append(item)
            used_bytes += item_bytes
        return {
            "ok": all(bool(result.get("ok")) for result in completed),
            "operation_count": len(operations),
            "results_returned": len(results),
            "results": results,
            "truncated": truncated,
        }

    return (
        workspace_info,
        list_files,
        glob_files,
        grep_files,
        read_file,
        inspect_workspace,
    )


def _run_inspect_operation(
    filesystem: WorkspaceFileSystem,
    operation: _InspectOperation,
) -> dict[str, object]:
    if operation.operation == "list":
        action = lambda: filesystem.list_files(
            operation.path,
            max_results=operation.max_results,
        )
    elif operation.operation == "glob":
        action = lambda: filesystem.glob_files(
            operation.pattern or "",
            path=operation.path,
            max_results=operation.max_results,
        )
    elif operation.operation == "grep":
        action = lambda: filesystem.grep_files(
            operation.query or "",
            path=operation.path,
            file_pattern=operation.file_pattern,
            case_sensitive=operation.case_sensitive,
            max_results=operation.max_results,
        )
    else:
        action = lambda: filesystem.read_file(
            operation.path,
            line_offset=operation.line_offset,
            n_lines=operation.n_lines,
        )
    return _recover_file_error(action)


def _recover_file_error(
    operation: Callable[[], dict[str, object]],
) -> dict[str, object]:
    try:
        return operation()
    except WorkspaceFileError as error:
        return error.as_result()


def _require_non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


__all__ = ["create_read_only_file_tools"]
