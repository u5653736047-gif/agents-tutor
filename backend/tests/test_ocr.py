"""OCR 底座测试（六大功能计划 P0-6 验收）。

覆盖：
1. off 模式强制关闭、非法模式抛错（配置错误暴露哲学）；
2. auto 模式依赖缺失时降级为 None 且不抛错（CI 必测路径——
   评委环境无 OCR 依赖时附件链路友好提示而非系统故障）；
3. provider 解析引擎输出的宽容性（不同版本返回形态）。
"""

from __future__ import annotations

import base64
import sys
import types
from typing import Any

import pytest

import core.ocr as ocr_module
from core.ocr import RapidOcrProvider, create_ocr_provider


def test_off_mode_returns_none_without_probing() -> None:
    assert create_ocr_provider("off") is None


def test_invalid_mode_raises_configuration_error() -> None:
    with pytest.raises(ValueError, match="API_OCR_MODE"):
        create_ocr_provider("yes")


def test_auto_mode_degrades_when_dependency_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """依赖缺失（ImportError）→ None，不抛错、不阻断启动。"""

    class _UnavailableProvider:
        def __init__(self) -> None:
            raise ImportError("rapidocr_onnxruntime not installed")

    monkeypatch.setattr(ocr_module, "RapidOcrProvider", _UnavailableProvider)

    assert create_ocr_provider("auto") is None


@pytest.mark.parametrize(
    "engine_return,expected",
    [
        # rapidocr 1.x 形态：(result, elapse)，result=[[box, text, score], ...]
        (
            ([[[0, 0, 1, 1], "你好世界", 0.99]], 0.1),
            "你好世界",
        ),
        # 多行：result 是多个 [box, text, score] 项
        (
            ([[[0, 0, 1, 1], "第一行", 0.9], [[0, 0, 1, 1], "第二行", 0.8]], 0.2),
            "第一行\n第二行",
        ),
        # 无识别结果：result 为 None
        ((None, 0.1), ""),
        # 脏项跳过、空白文本跳过
        (
            (
                [[[0, 0, 1, 1], "有效行", 0.9], "bad-item", [[0], "   ", 0.5]],
                0.3,
            ),
            "有效行",
        ),
        # dict 形态（审查 S2）：按字段名取 text，不再因下标取值 KeyError
        # 被吞而恒返回空串
        (
            ([{"text": "dict 形态行"}, {"text": "   "}, {"score": 0.1}], 0.2),
            "dict 形态行",
        ),
    ],
    ids=["single-line", "multi-line", "empty", "dirty-items", "dict-items"],
)
def test_provider_parses_engine_output_leniently(
    monkeypatch: pytest.MonkeyPatch,
    engine_return: Any,
    expected: str,
) -> None:
    fake_module = types.ModuleType("rapidocr_onnxruntime")

    class FakeRapidOCR:
        def __call__(self, image: bytes) -> Any:
            return engine_return

    fake_module.RapidOCR = FakeRapidOCR  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake_module)

    provider = RapidOcrProvider()

    assert provider.extract_text(b"fake-image-bytes") == expected


# ── 审查 W8：真实引擎跳过用例（依赖可选，未装环境自动跳过）───────

# 1x1 最小 PNG（透明像素）：验证真实引擎的「构造 + 调用」路径不抛错，
# 识别文本可能为空（验证的是调用链路而非识别精度）。
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_real_rapidocr_engine_path_when_extra_installed() -> None:
    """安装 ocr extra 后：真实惰性导入 + 引擎调用不抛错（审查 W8）。

    未安装环境 importorskip 自动跳过（CI 零影响）；本用例守护的是
    「生产演示时 OCR 首次真实调用」——真实引擎初始化、输入字节
    接受、返回值类型，这些是 fake module 测不到的路径。"""
    pytest.importorskip("rapidocr_onnxruntime")

    provider = RapidOcrProvider()

    result = provider.extract_text(_TINY_PNG)

    assert isinstance(result, str)
