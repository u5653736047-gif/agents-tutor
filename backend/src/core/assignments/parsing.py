"""学生作业 PDF 上传解析的纯函数。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pypdf import PdfReader

from .models import PageText, PdfInspection

_PDF_MAGIC = b"%PDF-"


def inspect_pdf(path: str | Path) -> PdfInspection:
    """打开 PDF 并逐页统计文本层与图片页，判定 PDF 类型。

    - text_based：全部页有文本层
    - mixed：部分页无文本层（空白/图片页）
    - image_based：全部页无文本层但含图片（扫描件/图片型）
    - scanned：全部页无文本层且无图片
    """
    source = Path(path)
    pages: list[PageText] = []
    text_pages = 0
    image_pages = 0
    try:
        reader = PdfReader(source)
        for page_number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            has_images = bool(getattr(page, "images", None))
            if text:
                text_pages += 1
            elif has_images:
                image_pages += 1
            pages.append(
                PageText(
                    page=page_number,
                    text=text or None,
                    char_count=len(text),
                )
            )
    except Exception as exc:
        raise ValueError(f"Cannot read PDF '{source.name}': {exc}") from exc

    blank_pages = len(pages) - text_pages
    pdf_type: Literal["text_based", "mixed", "scanned", "image_based"]
    if text_pages > 0 and blank_pages == 0:
        pdf_type = "text_based"
    elif text_pages > 0:
        pdf_type = "mixed"
    elif image_pages > 0:
        pdf_type = "image_based"
    else:
        pdf_type = "scanned"

    return PdfInspection(
        pdf_type=pdf_type,
        page_count=len(pages),
        text_pages=text_pages,
        blank_pages=blank_pages,
        image_pages=image_pages,
        pages=pages,
    )


def parse_upload(
    path: str | Path,
    *,
    upload_root: str | Path | None = None,
) -> dict[str, Any]:
    """识别上传文件并给出结构化解析结论（不含全文，防撑爆上下文）。

    无文本层 / 非 PDF 属于内容状态，返回 ok=False 的结构化结果；
    文件缺失、损坏或路径越界抛 ValueError（环境错误）。
    """
    source = _resolve_upload_path(path, upload_root)

    if source.suffix.lower() != ".pdf":
        return {
            "ok": False,
            "file_type": "unsupported",
            "message": "仅支持 PDF 文件，请上传文本型 PDF",
        }

    with source.open("rb") as handle:
        magic = handle.read(len(_PDF_MAGIC))
    if magic != _PDF_MAGIC:
        return {
            "ok": False,
            "file_type": "not_pdf",
            "message": "文件内容不是有效的 PDF",
        }

    inspection = inspect_pdf(source)
    if inspection.pdf_type in ("scanned", "image_based"):
        return {
            "ok": False,
            "file_type": "pdf",
            "pdf_type": inspection.pdf_type,
            "message": (
                "该 PDF 无文本层（可能为扫描件或图片型）。"
                "本期仅支持文本型 PDF，请转换为文本型后重试。"
            ),
        }

    message = (
        "文本型 PDF，可继续批改"
        if inspection.pdf_type == "text_based"
        else "部分页面无文本层（空白/图片页），可继续批改"
    )
    return {
        "ok": True,
        "file_type": "pdf",
        "pdf_type": inspection.pdf_type,
        "page_count": inspection.page_count,
        "text_pages": inspection.text_pages,
        "blank_pages": inspection.blank_pages,
        "image_pages": inspection.image_pages,
        "message": message,
        "pages": [
            {"page": item.page, "char_count": item.char_count}
            for item in inspection.pages
        ],
    }


def extract_pdf_text(
    path: str | Path,
    *,
    max_pages: int = 200,
    max_chars_per_page: int = 10_000,
) -> dict[str, Any]:
    """抽取 PDF 逐页文本，带页数与单页字符上限的防御性截断。"""
    inspection = inspect_pdf(path)
    if inspection.pdf_type in ("scanned", "image_based"):
        return {
            "ok": False,
            "message": (
                "该 PDF 无文本层（可能为扫描件或图片型）。"
                "本期仅支持文本型 PDF。"
            ),
        }

    truncated = False
    pages: list[dict[str, Any]] = []
    for item in inspection.pages[:max_pages]:
        text = item.text
        if text is not None and len(text) > max_chars_per_page:
            text = text[:max_chars_per_page] + f"\n...（单页超过 {max_chars_per_page} 字符，已截断）"
            truncated = True
        pages.append(
            {
                "page": item.page,
                "text": text,
                "char_count": item.char_count,
            }
        )
    if len(inspection.pages) > max_pages:
        truncated = True

    return {
        "ok": True,
        "pdf_type": inspection.pdf_type,
        "truncated": truncated,
        "pages": pages,
    }


def _resolve_upload_path(
    path: str | Path,
    upload_root: str | Path | None,
) -> Path:
    """解析路径并校验文件存在与上传目录边界。"""
    if not str(path).strip():
        raise ValueError("path must not be empty")
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"File not found: {resolved.name}")
    if upload_root is not None:
        root = Path(upload_root).expanduser().resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("路径不在允许的上传目录内")
    return resolved


__all__ = ["extract_pdf_text", "inspect_pdf", "parse_upload"]
