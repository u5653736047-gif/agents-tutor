"""可选视觉理解提供方（S5-B3）：OpenAI 兼容视觉端点 → 图片内容描述。

（面向初学者的设计说明，按功能模块）

1. 本模块的位置：附件图片理解链的第一级
   附件图片此前只有 OCR 一条路（提取文字）。本模块提供「理解链」的
   可选第一级：视觉语言模型（VLM）先描述图中内容/题目/公式，失败时
   降级到既有 OCR，再降级为友好提示——三级降级链，与仓库「可用才开」
   哲学一致。默认配置（未配置视觉端点）下行为与现状完全一致。

2. 为什么是 OpenAI 兼容端点而不是写死某家 VLM
   DeepSeek 主站模型不支持图片输入；自选视觉端点（如 Qwen-VL 的
   OpenAI 兼容接口）由部署方通过 env 接入——复用 langchain-openai
   客户端（base64 图片走 image_url 内容块），零新增 Python 依赖。

3. 调用预算约束（复用 S5 改写器轻量实例的教训）
   视觉调用是附件理解的辅助增强：timeout=10s、max_retries=0、返回
   描述有界截断——慢调用不得拖住消息主链路，超时/失败即沿降级链下沉。

4. 模式语义（与 create_ocr_provider 同一约定）
   - API_VISION_MODE=off：强制关闭；
   - auto（默认）：base_url/model/api_key 任一未配置 → None（不装配），
     配置了则构造（构造本身不发网络请求，首次调用才真正访问端点）；
   - 其它值：ValueError（配置拼写错误要暴露）。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Protocol

# 描述文本的有界上限：VLM 偶尔会长篇大论，截断保住上下文预算。
_MAX_DESCRIBE_CHARS = 2000

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_TOKENS = 512

_DESCRIBE_PROMPT = (
    "请简要描述这张图片中的内容：如果是题目请转录题面（含公式，用"
    " LaTeX 表示），如果是图表请说明数据要点，其他情况概述画面。"
    "只输出描述正文，不要开场白。"
)


class VisionProvider(Protocol):
    """视觉理解提供方协议：从图片字节产出文字描述（鸭子类型可替换）。"""

    def describe_image(self, image_bytes: bytes) -> str:
        """描述图片内容；失败时抛错（由调用方沿降级链下沉）。"""
        ...


def _image_mime_type(image_bytes: bytes) -> str:
    """按魔数嗅探 MIME 类型（data URL 需要）；未知格式默认 png。"""
    if image_bytes.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "image/png"


class OpenAICompatibleVisionProvider:
    """OpenAI 兼容视觉端点实现（Qwen-VL 等自选端点均可接入）。

    model 参数满足最小调用协议（invoke(messages) -> 有 .content 的
    回答），生产为 ChatOpenAI 轻量实例、测试注入替身——与
    LLMQueryRewriter 的模型注入模式一致。
    """

    def __init__(self, model: Any) -> None:
        self._model = model

    def describe_image(self, image_bytes: bytes) -> str:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        mime = _image_mime_type(image_bytes)
        from langchain_core.messages import HumanMessage

        message = HumanMessage(
            content=[
                {"type": "text", "text": _DESCRIBE_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ]
        )
        response = self._model.invoke([message])
        content = getattr(response, "content", "")
        if not isinstance(content, str):
            content = ""
        text = content.strip()
        if not text:
            raise ValueError("vision endpoint returned empty description")
        return text[:_MAX_DESCRIBE_CHARS]


def create_vision_provider(mode: str = "auto") -> VisionProvider | None:
    """按模式装配视觉理解提供方；不可用时返回 None（降级，不抛错）。

    - "off"：强制关闭；
    - "auto"（默认）：API_VISION_BASE_URL / API_VISION_MODEL /
      API_VISION_API_KEY 任一缺失 → None（默认部署零改动）；齐备则
      构造轻量 ChatOpenAI 实例（timeout=10s / max_retries=0 /
      max_tokens=512，见模块注释第 3 节）；
    - 其它值：ValueError（配置错误要暴露）。
    """
    if mode == "off":
        return None
    if mode != "auto":
        raise ValueError("API_VISION_MODE 只支持 auto 或 off")
    base_url = (os.getenv("API_VISION_BASE_URL") or "").strip()
    model_name = (os.getenv("API_VISION_MODEL") or "").strip()
    api_key = (os.getenv("API_VISION_API_KEY") or "").strip()
    if not base_url or not model_name or not api_key:
        # 未配置视觉端点：默认部署的关键路径，直接不装配（零改动）。
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None
    from pydantic import SecretStr

    model = ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=SecretStr(api_key),
        temperature=0,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        max_retries=0,
        max_tokens=_DEFAULT_MAX_TOKENS,  # type: ignore[call-arg]
    )
    return OpenAICompatibleVisionProvider(model)


__all__ = [
    "OpenAICompatibleVisionProvider",
    "VisionProvider",
    "create_vision_provider",
]
