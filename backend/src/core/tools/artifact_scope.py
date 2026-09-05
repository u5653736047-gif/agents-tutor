"""工作流产物区自动授权的作用域通道（lesson-workflow-design §五）。

三方共享：
- react_agent：执行工作流 Worker 的工具调用前，按 state.workflow 的
  artifact_root 进入作用域（写）；
- ToolExecutor.artifact_auto_approval_root：判定 officecli_edit 是否可
  免人工审批（读）；
- officecli_edit 运行时门：放行「全部涉文件都在产物根内」的写命令（读）。

只有 officecli_edit 参与豁免（shell 永不豁免：工作区授权不是系统级命
令沙箱）；产物根目录由工作流启动时新建，天然不含用户既有文件。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

artifact_auto_approval_roots: ContextVar[tuple[str, ...]] = ContextVar(
    "workflow_artifact_auto_approval_roots",
    default=(),
)


@contextmanager
def artifact_auto_approval(roots: Sequence[str]) -> Iterator[None]:
    """登记产物区自动授权根（仅工作流 Worker 执行路径使用）。"""
    token = artifact_auto_approval_roots.set(tuple(roots))
    try:
        yield
    finally:
        artifact_auto_approval_roots.reset(token)
