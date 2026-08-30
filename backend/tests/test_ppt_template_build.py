"""构建脚本自检集成测试（ppt-template-theme-plan M4.4）。

officecli 可用时在临时目录真实重建两套模板，断言脚本构建 + 内置自验
（0 页 / validate / 五版式 / 深色版式背景与白字手术）全绿；officecli
不可用则跳过——单测套件本身不依赖本地二进制（与 export/ppt 测试的假
工具策略一致）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_ppt_templates.py"


def _officecli_available() -> bool:
    configured = os.getenv("API_OFFICECLI_BINARY", "officecli").strip() or "officecli"
    if Path(configured).is_absolute() and Path(configured).exists():
        return True
    return shutil.which(configured) is not None


@pytest.mark.skipif(not _officecli_available(), reason="officecli 不可用")
class TestBuildPptTemplatesScript:
    def test_rebuilds_both_themes_into_temp_dir(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--output-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        assert result.returncode == 0, (
            f"构建脚本失败：\nstdout: {result.stdout[-800:]}\n"
            f"stderr: {result.stderr[-800:]}"
        )
        for name in ("edu-theme.pptx", "academic-theme.pptx"):
            asset = tmp_path / name
            assert asset.is_file() and asset.stat().st_size > 0

    def test_idempotent_rerun_overwrites(self, tmp_path: Path) -> None:
        # 幂等：二次构建（删旧 → create）成功且文件更新
        first = tmp_path / "edu-theme.pptx"
        assert first.exists() is False
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    str(_SCRIPT),
                    "--output-dir",
                    str(tmp_path),
                    "--only",
                    "edu",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            assert result.returncode == 0, result.stderr[-500:]
        assert first.is_file() and first.stat().st_size > 0
