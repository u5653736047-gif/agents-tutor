"""附件文本提取与消息组装（六大功能计划 P2-7）。

chat / stream 端点消费 ChatRequest.attachments（schemas.py 已预留契约
字段，此前路由忽略）：按 file_id 从 files.py 用户隔离目录读取，
.txt 直读 / .pdf 走 pypdf / 图片走 OCR provider，提取文本以结构化
分隔符拼入本轮用户消息——附件内容因此进入模型上下文与 checkpoint
持久化历史（不改 state schema、不改图结构）。

护栏与降级（pi 审查 🟡5）：
- 每附件提取文本上限 API_ATTACHMENT_MAX_CHARS（默认 30000 字符）、
  全部附件合计上限 100000 字符，超限截断并附「已截断」标注——与
  上下文预算（P0-1 的 512K）形成两道护栏：附件上限防单轮注入失控，
  上下文预算防历史累积失控；
- OCR 不可用（评委/CI 环境未装 ocr extra）：友好提示而非报错，
  不中断其余附件处理（core/ocr.py 的降级语义）；
- 跨用户/不存在的 file_id：该附件内容不进消息（files.py 的用户隔离
  目录规则天然拒绝），附「附件不可用」标注——不返回 403，与下载
  端点「不泄露目录存在性」同一口径。

无附件时行为与扩展前逐字节一致（返回原消息，零回归）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pypdf import PdfReader

from api.files import _is_safe_segment, _sanitize_user_key, _uploads_root
from api.schemas import Attachment
from core.ocr import OcrProvider
from core.pdf_table import PdfTableExtractor, open_pdf_table_extractor
from core.vision import VisionProvider

_LOGGER = logging.getLogger(__name__)

# 提取文本上限（pi 审查 🟡5）：env 可调，默认值见模块注释。
DEFAULT_ATTACHMENT_MAX_CHARS = 30000
DEFAULT_ATTACHMENTS_TOTAL_MAX_CHARS = 100000
# 附件数量上限（审查 W2）：与 ChatRequest.attachments 的 max_length=10
# 同源——本模块被直接调用（不经 HTTP 契约层）时仍受保护；超限部分
# 合并为单条「已忽略」标注，保证「附件不可用」标注段落总量也有界。
DEFAULT_MAX_ATTACHMENTS = 10

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
_OCR_UNAVAILABLE_HINT = (
    "当前部署未启用图片识别，请上传 txt/pdf 或让管理员启用 OCR"
    "（uv sync --extra ocr）"
)
_VISION_UNAVAILABLE_HINT = (
    "当前部署未启用图片理解，且图片中未识别出文本；"
    "如需图片内容分析请联系管理员配置视觉端点"
)


def _attachment_max_chars() -> int:
    """单附件提取上限（env API_ATTACHMENT_MAX_CHARS，非法回退默认）。"""
    raw = os.getenv("API_ATTACHMENT_MAX_CHARS")
    if raw is None or not raw.strip():
        return DEFAULT_ATTACHMENT_MAX_CHARS
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_ATTACHMENT_MAX_CHARS
    return value if value > 0 else DEFAULT_ATTACHMENT_MAX_CHARS


def _attachments_total_max_chars() -> int:
    """全部附件合计上限（env API_ATTACHMENTS_TOTAL_MAX_CHARS）。"""
    raw = os.getenv("API_ATTACHMENTS_TOTAL_MAX_CHARS")
    if raw is None or not raw.strip():
        return DEFAULT_ATTACHMENTS_TOTAL_MAX_CHARS
    try:
        value = int(raw.strip())
    except ValueError:
        return DEFAULT_ATTACHMENTS_TOTAL_MAX_CHARS
    return value if value > 0 else DEFAULT_ATTACHMENTS_TOTAL_MAX_CHARS


def _resolve_attachment_path(file_id: str, user_id: str | None) -> Path | None:
    """file_id → 用户隔离磁盘路径；非法段/跨用户/不存在一律 None。

    与 files.py 下载端点同一口径：user_key 由当前 X-User-Id 消毒得出，
    他人目录下的 uuid 文件名不可枚举，等价于文件不存在（不泄露目录
    存在性）。
    """
    if not _is_safe_segment(file_id):
        return None
    path = _uploads_root() / _sanitize_user_key(user_id) / file_id
    return path if path.is_file() else None


def _pdf_table_mode() -> str:
    """PDF 表格提取模式（env API_PDF_TABLE_MODE，auto|off）。

    在进入单附件提取的宽容 try 之前调用并校验：配置拼写错误要暴露
    （ValueError 直接上抛），不能被吞成「附件内容提取失败」。
    """
    raw = os.getenv("API_PDF_TABLE_MODE")
    if raw is None or not raw.strip():
        return "auto"
    value = raw.strip()
    if value not in {"auto", "off"}:
        raise ValueError("API_PDF_TABLE_MODE 只支持 auto 或 off")
    return value


def _extract_image_text(
    image_bytes: bytes,
    ocr_provider: OcrProvider | None,
    vision_provider: VisionProvider | None,
) -> str:
    """图片三级降级链（S5-B3）：VLM 理解 → OCR 文字 → 友好提示。

    每一级失败（异常/空结果）都沉降到下一级，任何一级成功即返回——
    视觉端点故障不影响既有 OCR 行为，OCR 缺失或引擎故障都落到友好
    提示兜底，不把「引擎故障」误报成「文件损坏」。
    """
    if vision_provider is not None:
        try:
            description = vision_provider.describe_image(image_bytes)
            if description.strip():
                return description
        except Exception as exc:  # noqa: BLE001 - 降级链：视觉失败沉降至 OCR
            # 只记异常类型不打响应体（可能含敏感信息）；持续故障时运维
            # 可从日志发现「配置了视觉端点却永远在走 OCR」。
            _LOGGER.warning(
                "视觉端点描述失败，降级到 OCR：%s", type(exc).__name__
            )
    if ocr_provider is not None:
        try:
            text = ocr_provider.extract_text(image_bytes)
        except Exception as exc:  # noqa: BLE001 - 降级链：OCR 故障落到提示级
            _LOGGER.warning("OCR 提取失败，降级到友好提示：%s", type(exc).__name__)
        else:
            if text.strip():
                return text
    return _VISION_UNAVAILABLE_HINT


def _extract_text(
    path: Path,
    ocr_provider: OcrProvider | None,
    *,
    char_limit: int,
    table_extractor: PdfTableExtractor | None = None,
    vision_provider: VisionProvider | None = None,
) -> str:
    """按扩展名提取附件文本；提取失败返回带原因的标注文本。

    char_limit（审查 S1）：PDF 逐页累计、达到上限即停止解析——截断
    发生在提取过程中而非提取之后，避免大 PDF 全量解析自白耗 CPU
    （500 页教材只需解析到满足 30K 字符的页数即停）。

    table_extractor（S5-B1）：非 None 时每页文本后附「[表格]」Markdown
    小节；表格文本计入同一 char_limit 预算（护栏不因增强而放宽）。

    vision_provider（S5-B3）：图片分支三级降级链的第一级，None 时与
    现状一致（OCR → 友好提示）。
    """
    ext = path.suffix.lower()
    try:
        if ext == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")
        if ext == ".pdf":
            reader = PdfReader(path)
            chunks: list[str] = []
            size = 0
            for page_index, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                if table_extractor is not None:
                    tables = table_extractor.page_tables_markdown(page_index)
                    if tables:
                        text = f"{text}\n\n{tables}" if text else tables
                chunks.append(text)
                size += len(text)
                if size >= char_limit:
                    break
            return "\n".join(chunks)
        if ext in _IMAGE_EXTENSIONS:
            return _extract_image_text(
                path.read_bytes(), ocr_provider, vision_provider
            )
    except Exception:  # noqa: BLE001 - 单附件提取失败不中断其余附件
        return "附件内容提取失败（文件可能损坏或格式异常）"
    return "不支持的附件类型"


def compose_message_with_attachments(
    message: str,
    attachments: list[Attachment] | None,
    user_id: str | None,
    ocr_provider: OcrProvider | None,
    vision_provider: VisionProvider | None = None,
) -> str:
    """把附件提取文本拼入用户消息；无附件时原样返回（零回归）。

    组装格式：原消息 + 每附件一段「[附件 N：文件名（类型标注）]\n文本」，
    段落间用分隔线隔开——模型看到的是结构化、有边界的材料，批改/
    答疑约定（prompts.py evaluator 卡）据此识别答案材料与作业正文。
    """
    if not attachments:
        return message
    # 审查 W2：数量防御截断——契约层（max_length=10）之外的直接调用
    # 路径同样受保护；超限部分合并为单条标注，段落总量有界。
    if len(attachments) > DEFAULT_MAX_ATTACHMENTS:
        dropped = len(attachments) - DEFAULT_MAX_ATTACHMENTS
        attachments = attachments[:DEFAULT_MAX_ATTACHMENTS]
        overflow_note = f"[已忽略 {dropped} 个超出数量上限的附件]"
    else:
        overflow_note = None
    per_attachment_limit = _attachment_max_chars()
    total_limit = _attachments_total_max_chars()
    # S5-B1：模式校验在宽容 try 之外（配置错误直接暴露，不吞成
    # 「附件内容提取失败」）；提取器按 PDF 附件惰性打开，off /
    # 未装 pdfplumber 时为 None → 行为与现状逐项一致。
    pdf_table_mode = _pdf_table_mode()
    sections: list[str] = []
    total_chars = 0
    for index, attachment in enumerate(attachments, start=1):
        path = _resolve_attachment_path(attachment.file_id, user_id)
        if path is None:
            sections.append(
                f"[附件 {index}：{attachment.name}]\n"
                "该附件不可用（文件不存在或无访问权限），已忽略其内容。"
            )
            continue
        extractor = (
            open_pdf_table_extractor(path, mode=pdf_table_mode)
            if path.suffix.lower() == ".pdf"
            else None
        )
        try:
            text = _extract_text(
                path,
                ocr_provider,
                char_limit=per_attachment_limit,
                table_extractor=extractor,
                vision_provider=vision_provider,
            )
        finally:
            if extractor is not None:
                extractor.close()
        if len(text) > per_attachment_limit:
            text = (
                text[:per_attachment_limit]
                + f"\n[已截断，仅前 {per_attachment_limit} 字符参与处理]"
            )
        remaining = total_limit - total_chars
        if remaining <= 0:
            sections.append(
                f"[附件 {index}：{attachment.name}]\n"
                f"[已截断：附件总量上限 {total_limit} 字符已用尽，本附件未纳入]"
            )
            continue
        if len(text) > remaining:
            text = (
                text[:remaining]
                + f"\n[已截断，附件总量上限 {total_limit} 字符]"
            )
        total_chars += len(text)
        header = f"[附件 {index}：{attachment.name}]"
        if path.suffix.lower() in _IMAGE_EXTENSIONS:
            header += "（机器识别文本，可能存在识别误差）"
        sections.append(f"{header}\n{text}")
    if overflow_note is not None:
        sections.append(overflow_note)
    if not sections:
        return message
    return (
        f"{message}\n\n"
        + "\n---\n".join(sections)
        + "\n---\n请结合以上附件材料处理本次请求。"
    )


__all__ = ["compose_message_with_attachments"]
