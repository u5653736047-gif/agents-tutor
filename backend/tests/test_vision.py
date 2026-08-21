"""S5-B3 视觉理解可选接入测试（core/vision.py + 附件三级降级链）。

覆盖清单：
1. 工厂模式分支：off → None；auto 未配置端点 → None（默认部署零改动）；
   非法值 → ValueError；
2. OpenAICompatibleVisionProvider：成功描述（替身模型，校验 base64
   图片内容块）；空回答抛错；超长描述有界截断；
3. 附件三级降级链：VLM 成功 → 用描述；VLM 失败 → 沉降 OCR；VLM 与
   OCR 均不可用 → 友好提示；OCR 失败但 VLM 成功 → 不受影响。
"""

from __future__ import annotations

import base64
from pathlib import Path

from pytest import MonkeyPatch

from api.attachments import compose_message_with_attachments
from api.schemas import Attachment
from core.vision import (
    OpenAICompatibleVisionProvider,
    create_vision_provider,
)


class _FakeVisionModel:
    """视觉模型替身：记录调用、返回预定文本或抛错。"""

    def __init__(self, text: str = "图中是一道求极限的题目", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[list] = []

    def invoke(self, messages: list) -> object:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        from langchain_core.messages import AIMessage

        return AIMessage(content=self.text)


def _attachment(file_id: str, name: str) -> Attachment:
    return Attachment(file_id=file_id, name=name, content_type=None, size=1)


_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-payload"


# ── 1. 工厂模式分支 ───────────────────────────────────────────────


def test_create_vision_provider_off_returns_none(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_VISION_MODE", "off")
    assert create_vision_provider("off") is None


def test_create_vision_provider_auto_without_config_returns_none(
    monkeypatch: MonkeyPatch,
) -> None:
    """默认部署零改动：未配置端点时 auto 不装配。"""
    for var in ("API_VISION_BASE_URL", "API_VISION_MODEL", "API_VISION_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert create_vision_provider("auto") is None


def test_create_vision_provider_invalid_mode_raises() -> None:
    try:
        create_vision_provider("auot")
    except ValueError as exc:
        assert "API_VISION_MODE" in str(exc)
    else:
        raise AssertionError("invalid mode must raise ValueError")


# ── 2. Provider 行为 ──────────────────────────────────────────────


def test_vision_provider_sends_base64_image_and_returns_description() -> None:
    model = _FakeVisionModel()
    provider = OpenAICompatibleVisionProvider(model)

    description = provider.describe_image(_PNG_BYTES)

    assert description == "图中是一道求极限的题目"
    content = model.calls[0][0].content
    assert isinstance(content, list)
    image_block = content[1]
    encoded = base64.b64encode(_PNG_BYTES).decode()
    assert image_block["image_url"]["url"].endswith(encoded)
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_provider_empty_answer_raises() -> None:
    provider = OpenAICompatibleVisionProvider(_FakeVisionModel(text=""))
    try:
        provider.describe_image(_PNG_BYTES)
    except ValueError:
        pass
    else:
        raise AssertionError("empty answer must raise")


def test_vision_provider_truncates_long_description() -> None:
    provider = OpenAICompatibleVisionProvider(_FakeVisionModel(text="长" * 5000))

    assert len(provider.describe_image(_PNG_BYTES)) == 2000


# ── 3. 附件三级降级链 ─────────────────────────────────────────────


def _write_image(root: Path, user_key: str, file_id: str) -> None:
    target = root / user_key / file_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_PNG_BYTES)


class _FakeOcr:
    def __init__(self, text: str = "OCR 识别文本") -> None:
        self.text = text

    def extract_text(self, image_bytes: bytes) -> str:
        return self.text


def test_image_uses_vision_description_when_available(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_image(tmp_path, "u1", "img.png")
    vision = OpenAICompatibleVisionProvider(_FakeVisionModel())

    composed = compose_message_with_attachments(
        "看图", [_attachment("img.png", "题.png")], "u1", None, vision
    )

    assert "图中是一道求极限的题目" in composed


def test_image_falls_back_to_ocr_when_vision_fails(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_image(tmp_path, "u1", "img.png")
    broken_vision = OpenAICompatibleVisionProvider(
        _FakeVisionModel(error=RuntimeError("endpoint down"))
    )

    composed = compose_message_with_attachments(
        "看图", [_attachment("img.png", "题.png")], "u1", _FakeOcr(), broken_vision
    )

    assert "OCR 识别文本" in composed


def test_image_friendly_hint_when_both_unavailable(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_image(tmp_path, "u1", "img.png")

    composed = compose_message_with_attachments(
        "看图", [_attachment("img.png", "题.png")], "u1", None, None
    )

    assert "图片理解" in composed or "图片识别" in composed


def test_image_vision_success_does_not_touch_ocr(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """第一级成功即返回：OCR 不被调用（避免无谓的双重推理开销）。"""
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    _write_image(tmp_path, "u1", "img.png")

    class _CountingOcr:
        called = False

        def extract_text(self, image_bytes: bytes) -> str:
            type(self).called = True
            return "不应出现的 OCR 文本"

    vision = OpenAICompatibleVisionProvider(_FakeVisionModel())
    ocr = _CountingOcr()

    composed = compose_message_with_attachments(
        "看图", [_attachment("img.png", "题.png")], "u1", ocr, vision
    )

    assert "图中是一道求极限的题目" in composed
    assert not _CountingOcr.called
