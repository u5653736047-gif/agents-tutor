"""Approval-gated foreground shell tool with bounded streaming output."""

from __future__ import annotations

import codecs
import os
import queue
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import monotonic

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..filesystem import (
    WorkspaceFileError,
    WorkspaceFileSystem,
    active_workspace_filesystem,
)

DEFAULT_SHELL_TIMEOUT_SECONDS = 30
MAX_SHELL_TIMEOUT_SECONDS = 120
MAX_SHELL_OUTPUT_BYTES = 256 * 1024
_READ_SIZE = 4096
_APPROVED_SHELL: ContextVar[bool] = ContextVar("approved_shell_execution", default=False)
_SHELL_OUTPUT: ContextVar[Callable[[str, str], None] | None] = ContextVar(
    "shell_output_callback",
    default=None,
)


class ShellInput(BaseModel):
    """One exact foreground command presented to the user for approval."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=8_000)
    cwd: str = Field(default=".", min_length=1, max_length=4_096)
    timeout_seconds: int = Field(
        default=DEFAULT_SHELL_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_SHELL_TIMEOUT_SECONDS,
    )
    description: str = Field(default="", max_length=200)

    @field_validator("command", "cwd")
    @classmethod
    def reject_blank_or_nul(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("value must not be blank and may not contain NUL")
        return value


@contextmanager
def approved_shell_execution() -> Iterator[None]:
    """Allow shell invocation only while an already-approved graph gate runs."""
    token = _APPROVED_SHELL.set(True)
    try:
        yield
    finally:
        _APPROVED_SHELL.reset(token)


@contextmanager
def shell_output_scope(callback: Callable[[str, str], None]) -> Iterator[None]:
    """Bind a stdout/stderr callback for incremental terminal events."""
    token = _SHELL_OUTPUT.set(callback)
    try:
        yield
    finally:
        _SHELL_OUTPUT.reset(token)


def create_shell_tool(filesystem: WorkspaceFileSystem) -> BaseTool:
    """Create a shell capability whose invocation is useless without approval."""

    def current() -> WorkspaceFileSystem:
        return active_workspace_filesystem(filesystem)

    @tool(
        "shell",
        args_schema=ShellInput,
        extras={
            "category": "terminal",
            "requires_approval": True,
            "status_from_ok": True,
        },
    )
    def shell(
        command: str,
        cwd: str = ".",
        timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS,
        description: str = "",
    ) -> dict[str, object]:
        """运行一条需用户批准的前台复合命令；支持管道和顺序命令，不支持交互输入。"""
        del description  # only shown in the approval card; never interpreted
        if not _APPROVED_SHELL.get():
            raise PermissionError("shell execution requires an explicit user approval")
        try:
            working_directory = current().resolve_directory(cwd)
        except WorkspaceFileError as error:
            raise RuntimeError(str(error)) from error
        return _run_foreground_shell(
            command,
            cwd=working_directory,
            timeout_seconds=timeout_seconds,
            on_output=_SHELL_OUTPUT.get(),
        )

    return shell


def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            raise RuntimeError("PowerShell is not available")
        utf8_prefix = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "$OutputEncoding = [Console]::OutputEncoding; "
        )
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"{utf8_prefix}{command}",
        ]
    return ["/bin/sh", "-lc", command]


def _run_foreground_shell(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: int,
    on_output: Callable[[str, str], None] | None,
) -> dict[str, object]:
    process = subprocess.Popen(
        _shell_argv(command),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        ),
        start_new_session=os.name != "nt",
    )
    if process.stdout is None or process.stderr is None:
        _terminate_process_tree(process)
        raise RuntimeError("shell output pipes are unavailable")

    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    readers = [
        threading.Thread(
            target=_read_pipe,
            args=(process.stdout, "stdout", output_queue),
            daemon=True,
        ),
        threading.Thread(
            target=_read_pipe,
            args=(process.stderr, "stderr", output_queue),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
    remaining_bytes = MAX_SHELL_OUTPUT_BYTES
    truncated = False
    completed_readers = 0
    timed_out = False
    started_at = monotonic()

    while completed_readers < len(readers):
        if not timed_out and monotonic() - started_at > timeout_seconds:
            timed_out = True
            _terminate_process_tree(process)
        try:
            channel, chunk = output_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        if chunk is None:
            completed_readers += 1
            continue
        bounded, used, was_truncated = _fit_utf8(chunk, remaining_bytes)
        remaining_bytes -= used
        truncated = truncated or was_truncated
        if bounded:
            captured[channel].append(bounded)
            if on_output is not None:
                on_output(channel, bounded)

    for reader in readers:
        reader.join(timeout=1)
    try:
        exit_code = process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        exit_code = process.wait(timeout=2)

    return {
        "ok": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "stdout": "".join(captured["stdout"]),
        "stderr": "".join(captured["stderr"]),
        "cwd": str(cwd),
        "timed_out": timed_out,
        "truncated": truncated,
    }


def _read_pipe(
    pipe: object,
    channel: str,
    output_queue: queue.Queue[tuple[str, str | None]],
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        file_descriptor = pipe.fileno()  # type: ignore[attr-defined]
        while True:
            data = os.read(file_descriptor, _READ_SIZE)
            if not data:
                break
            text = decoder.decode(data)
            if text:
                output_queue.put((channel, text))
        final = decoder.decode(b"", final=True)
        if final:
            output_queue.put((channel, final))
    finally:
        try:
            pipe.close()  # type: ignore[attr-defined]
        finally:
            output_queue.put((channel, None))


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
        return
    kill_process_group = getattr(os, "killpg", None)
    sigkill = getattr(signal, "SIGKILL", None)
    if not callable(kill_process_group) or not isinstance(sigkill, int):
        process.kill()
        return
    try:
        kill_process_group(process.pid, sigkill)
    except (OSError, ProcessLookupError):
        process.kill()


def _fit_utf8(text: str, budget: int) -> tuple[str, int, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, len(encoded), False
    if budget <= 0:
        return "", 0, True
    bounded = encoded[:budget].decode("utf-8", errors="ignore")
    return bounded, len(bounded.encode("utf-8")), True


__all__ = [
    "DEFAULT_SHELL_TIMEOUT_SECONDS",
    "MAX_SHELL_TIMEOUT_SECONDS",
    "ShellInput",
    "approved_shell_execution",
    "create_shell_tool",
    "shell_output_scope",
]
