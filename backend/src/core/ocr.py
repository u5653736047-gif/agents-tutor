"""OCR 底座（六大功能计划 P0-6）：图片附件的文本提取能力。

（面向初学者的设计说明）

1. 为什么需要这个模块
   上传管线已支持 .png/.jpg/.jpeg（api/files.py 白名单），但批改
   （手写作业照片）与答疑（手写公式照片）需要把图片变成文本才能
   进入模型上下文。本模块提供 OCR provider 抽象与轻量实现。

2. 装配哲学：复刻 fastembed 先例（app.py 的 API_KNOWLEDGE_EMBEDDING）
   - OCR 依赖是**可选依赖组**（pyproject 的 `ocr` extra，
     `uv sync --extra ocr` 启用）——默认 `uv sync --extra dev` 不安装，
     CI 与评委环境零重依赖；
   - `API_OCR_MODE=auto|off`：auto（默认）= 探测到依赖才启用，
     构造失败（ImportError / 模型文件异常）静默降级为不可用；off =
     强制关闭。配置错误（拼写）要暴露而非静默当成 auto；
   - 降级语义：provider 为 None 时调用方（附件提取链路）返回友好
     提示而非报错——图片缺 OCR 不是系统故障。

3. 实现选型（评审意见点名 PaddleOCR，经分析调整为轻量同源方案）
   默认 rapidocr-onnxruntime：PP-OCR 同源识别模型（中文识别同源同
   精度档）、纯 pip 约 50MB、模型文件内置于 wheel 无需联网下载、
   Windows/CI 无 paddlepaddle 环境坑。PaddleOCR 全家桶（PP-Structure
   表格识别等更高阶能力）留作同接口可替换 provider——OcrProvider 是
   鸭子类型协议，替换实现不动调用方。拒绝云 OCR API / VLM 直读
   （计划拒绝方案 9）：外部 API key 依赖违背「零外部依赖即可离线
   测试」的装配哲学。
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol


class OcrProvider(Protocol):
    """OCR 提供方协议：从图片字节提取文本（鸭子类型，可替换实现）。"""

    def extract_text(self, image_bytes: bytes) -> str:
        """提取图片中的文本；无可识别内容时返回空字符串。"""
        ...


class RapidOcrProvider:
    """rapidocr-onnxruntime 实现（默认；选型理由见模块注释第 3 节）。

    构造时惰性导入并初始化引擎——rapidocr_onnxruntime 未安装时抛
    ImportError，由装配方（create_ocr_provider）捕获降级，不阻断启动
    （与 FastEmbedProvider 的惰性导入同一模式）。
    """

    def __init__(self) -> None:
        # 惰性导入（与 FastEmbedProvider 的 importlib 先例同款，避免
        # 顶层 import 让 mypy/未安装环境报错）：可选依赖未安装时
        # ImportError 由装配方降级捕获。
        rapidocr_module: Any = importlib.import_module("rapidocr_onnxruntime")
        self._engine = rapidocr_module.RapidOCR()

    def extract_text(self, image_bytes: bytes) -> str:
        # rapidocr 1.x：engine(img) -> (result, elapse)，result 为
        # [[box, text, score], ...] 或 None；宽容处理不同版本的返回
        # 形态（2.x 的 OcrResult 同样按行迭代取文本字段）。
        raw: Any = self._engine(image_bytes)
        result = raw[0] if isinstance(raw, tuple) and raw else raw
        lines: list[str] = []
        for item in result or []:
            # 逐项宽容：引擎可能混入脏项。dict 形态按字段名取文本
            #（审查 S2：原实现放行 dict 却用 item[1] 下标取值，对 dict
            # 必然 KeyError 被吞——若引擎返回 dict 形态会恒返回空串）；
            # 序列形态取第 2 位（[box, text, score]）；其余跳过。
            if isinstance(item, dict):
                text = item.get("text")
            elif isinstance(item, (list, tuple)) and len(item) > 1:
                text = item[1]
            else:
                continue
            if isinstance(text, str) and text.strip():
                lines.append(text.strip())
        return "\n".join(lines)


def create_ocr_provider(mode: str = "auto") -> OcrProvider | None:
    """按模式装配 OCR provider；不可用时返回 None（降级，不抛错）。

    模式语义与 API_KNOWLEDGE_EMBEDDING 同一约定（app.py:86-92 先例）：
    - "auto"（默认）：尝试构造 RapidOcrProvider，依赖缺失/初始化失败
      （ImportError / RuntimeError / OSError）→ None；
    - "off"：强制关闭，不构造、不探测（评委/CI 显式禁用的口径）；
    - 其它值：配置错误要暴露而不是静默当成 auto（与 embedding 模式
      校验同一哲学：拼写错误应让运维立刻发现）。
    """
    if mode == "off":
        return None
    if mode != "auto":
        raise ValueError("API_OCR_MODE 只支持 auto 或 off")
    try:
        return RapidOcrProvider()
    except (ImportError, RuntimeError, OSError):
        # 未安装 rapidocr-onnxruntime / 模型初始化失败 → 降级不可用，
        # 不阻断启动（调用方返回友好提示，见模块注释第 2 节）。
        return None


__all__ = ["OcrProvider", "RapidOcrProvider", "create_ocr_provider"]
