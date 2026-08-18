"""真实 officecli 集成 smoke（计划 T4-1）。

在临时工作区中走完「创建 → batch 写 20 个单元格（回归 M6 大 token 放行）
→ inspect 读取校验（含 view text --range）→ validate」全链路，全部通过
输出 PASS 并以 0 退出；任一步失败抛 RuntimeError 非零退出。

用法：
    uv run python scripts/verify_officecli_integration.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from core.filesystem import WorkspaceFileSystem
from core.tools.office_tools import (
    OfficeCliSettings,
    approved_office_execution,
    create_office_tools,
    resolve_officecli_binary,
)


def _require_ok(step: str, result: dict[str, object]) -> None:
    """任一步失败即中止，并把子进程 stderr 带出来便于诊断。"""
    if result.get("ok") is not True:
        raise RuntimeError(f"{step} 失败：{result}")


def main() -> None:
    """按 T4-1 步骤执行 smoke，输出 PASS。"""
    binary = resolve_officecli_binary()
    with tempfile.TemporaryDirectory(prefix="officecli-smoke-") as tmp:
        workspace = Path(tmp) / "workspace"
        workspace.mkdir()
        filesystem = WorkspaceFileSystem(workspace)
        inspect_tool, edit_tool = create_office_tools(
            filesystem,
            OfficeCliSettings(binary=binary),
        )

        with approved_office_execution():
            # 1. 创建空工作簿
            _require_ok(
                "create 成绩单.xlsx",
                edit_tool.invoke({"command": ["create", "成绩单.xlsx"]}),
            )
            # 2. batch 一次写入 20 个单元格（A1:B10），同时回归 M6：
            # 20 条命令的 JSON 超过单 token 2K 上限，必须按 32K 大 token 放行
            batch_items = [
                {
                    "command": "set",
                    "path": f"/Sheet1/{column}{row}",
                    "props": {"value": value},
                }
                for row in range(1, 11)
                for column, value in (
                    # 备注栏填长文本，确保整个 batch JSON 超过 2K 字符
                    ("A", f"学生{row}·" + "进步显著" * 24),
                    ("B", str(60 + row)),
                )
            ]
            batch_json = json.dumps(batch_items, ensure_ascii=False)
            assert len(batch_json) > 2_000, "用例必须超过 2K 才能回归 M6"
            _require_ok(
                "batch 写入 20 个单元格",
                edit_tool.invoke(
                    {"command": ["batch", "成绩单.xlsx", "--commands", batch_json]}
                ),
            )

        # 3. 只读读取校验（含 view text --range 限定范围）
        viewed = inspect_tool.invoke(
            {"command": ["view", "成绩单.xlsx", "text", "--range", "Sheet1!A1:B10"]}
        )
        _require_ok("view text --range", viewed)
        stdout = str(viewed["stdout"])
        if "学生1" not in stdout or "61" not in stdout or "学生10" not in stdout:
            raise RuntimeError(f"view 内容校验失败：{stdout[:500]}")

        # 4. validate 结构校验
        _require_ok(
            "validate 成绩单.xlsx",
            inspect_tool.invoke({"command": ["validate", "成绩单.xlsx"]}),
        )

    print(f"binary={binary}")
    print("cells=20 (batch, >2K JSON token)")
    print("view/validate=ok")
    print("PASS")


if __name__ == "__main__":
    main()
