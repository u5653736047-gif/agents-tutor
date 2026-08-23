"""Load local text and PDF files into knowledge documents."""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import KnowledgeDocument

if TYPE_CHECKING:
    from ..ocr import OcrProvider


def load_text(
    path: str | Path,
    *,
    document_id: str | None = None,
    source_label: str | None = None,
) -> list[KnowledgeDocument]:
    """Load one non-empty UTF-8 text file."""
    source = Path(path)
    public_source = _public_source(source, source_label)
    content = source.read_text(encoding="utf-8").strip()
    # 空文件直接拒绝入库（空文档没有检索价值）。
    if not content:
        raise ValueError(f"Text file '{source.name}' is empty")

    return [
        KnowledgeDocument(
            document_id=document_id if document_id is not None else _default_document_id(source),
            content=content,
            source=public_source,
        )
    ]


def load_pdf(
    path: str | Path,
    *,
    document_id: str | None = None,
    source_label: str | None = None,
) -> list[KnowledgeDocument]:
    """Load each non-empty PDF page as a separate document.

    内部复用 iter_pdf_pages 的同一套逐页解析逻辑（错误消息完全一致）。
    """
    return list(
        iter_pdf_pages(path, document_id=document_id, source_label=source_label)
    )


def iter_pdf_pages(
    path: str | Path,
    *,
    document_id: str | None = None,
    source_label: str | None = None,
    progress: Callable[[int, int], None] | None = None,
    ocr_provider: OcrProvider | None = None,
    page_range: tuple[int, int] | None = None,
) -> Iterator[KnowledgeDocument]:
    """惰性逐页解析 PDF，产出每个非空页对应的 KnowledgeDocument。

    与 load_pdf 语义一致（document_id / source 映射、空页跳过、错误消息），
    差别在于：
    1. 逐页 yield，不一次性把所有页文本留在内存，适合 190MB 级别的大文件；
    2. 支持 progress(page, total) 进度回调，批量入库脚本用它打印解析进度；
    3. 解析/提取异常在迭代过程中抛出（同样包装为含文件名的 ValueError）。

    ocr_provider（S5-C3）：可选的扫描页兑底——页面无文本层时渲染为图片
    交由 provider 提取文本，产出的文档 metadata 携带 {"extraction": "ocr"}
    标记（供检索/评价侧辨别低置信内容）。OCR 不可用、渲染异常、识别为空
    一律静默跳过该页（与无文本层的现状一致），不抛错——OCR 是增强而
    非正确性前提。

    page_range（S5-C3）：可选的闭区间页码过滤 [start, end]（1 起，
    含两端）——仅 yield 范围内的页面；progress 回调仍遍历全书（total
    恒为全书页数，进度条不失真）。非法区间（start < 1 或 end < start）
    直接 ValueError（离线脚本场景配置错误要暴露）。
    """
    # 惰性导入：纯文本加载路径不依赖 pypdf。
    from pypdf import PdfReader

    # S5-B2：表格结构化提取（可选增强）。API_PDF_TABLE_MODE=off 或
    # 未装 pdfplumber 时 extractor 为 None → 行为与现状逐项一致；
    # 模式值非法在此处直接抛 ValueError（离线脚本场景配置错误要暴露）。
    from ..pdf_table import open_pdf_table_extractor, resolve_pdf_table_mode

    source = Path(path)
    public_source = _public_source(source, source_label)
    # S5-C3：分段页码过滤的参数校验（外层 try 之前，错误不被包装成
    # 「Cannot extract text」，配置错误要裸露清晰）。
    if page_range is not None:
        range_start_check, range_end_check = page_range
        if range_start_check < 1 or range_end_check < range_start_check:
            raise ValueError(
                f"Invalid page_range {page_range!r}: expected 1-based "
                "inclusive A-B with A <= B"
            )
    # 校验单一来源：与 api 层同一 resolve（非法值 ValueError 在离线
    # 脚本场景直接暴露）。
    table_mode = resolve_pdf_table_mode()
    extractor = open_pdf_table_extractor(source, mode=table_mode)
    # S5-C3：OCR 兜底的惰性 pypdfium2 文档句柄（首次遇到无文本层页才
    # 打开；导入/打开失败置位标志后不再重试——每页重复尝试导入只会
    # 白白浪费 CPU，行为上等价于无 OCR）。
    pdfium_doc: Any | None = None
    pdfium_unavailable = False

    def _ocr_page_text(page_index: int) -> str:
        """渲染指定页（0 起）为 PNG 并经 provider 提取文本；失败返回空串。"""
        nonlocal pdfium_doc, pdfium_unavailable
        if pdfium_unavailable:
            return ""
        # 调用点已守卫 provider 非 None；此处断言仅供 mypy 收窄类型。
        assert ocr_provider is not None
        try:
            if pdfium_doc is None:
                # pypdfium2 无类型存根（与 fastembed 等可选依赖同一处理）。
                import pypdfium2  # type: ignore[import-untyped]

                pdfium_doc = pypdfium2.PdfDocument(str(source))
        except Exception:  # noqa: BLE001 - 可选依赖探测：失败置位降级
            # 导入/打开失败置位标志：后续无文本页不再重复尝试（import
            # 虽有模块缓存，打开失败的书每次重试仍是白费的开销）。
            pdfium_unavailable = True
            return ""
        page = pdfium_doc[page_index]
        try:
            # scale=2.0 ≈ 144dpi：扫描版教材文字的最小可识别密度。
            image = page.render(scale=2.0).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="png")
            return ocr_provider.extract_text(buffer.getvalue()) or ""
        except Exception:  # noqa: BLE001 - 兜底路径：任何失败都等价于无文本
            return ""

    try:
        try:
            reader = PdfReader(source)
        except Exception as exc:
            raise ValueError(f"Cannot read PDF '{source.name}': {exc}") from exc

        # 整份 PDF 的所有页共用一个 document_id，页与页之间靠 page 字段区分。
        resolved_id = (
            document_id if document_id is not None else _default_document_id(source)
        )
        total_pages = len(reader.pages)
        # S5-C3：分段入库的页码过滤（闭区间，1 起）。
        range_start = range_end = 0
        if page_range is not None:
            range_start, range_end = page_range
        found_nonempty = False
        try:
            for page_number, page in enumerate(reader.pages, start=1):  # 页码从 1 开始
                if progress is not None:
                    progress(page_number, total_pages)
                if page_range is not None and not (
                    range_start <= page_number <= range_end
                ):
                    continue  # 分段模式：范围外页面不产出（progress 照常上报）
                content = (page.extract_text() or "").strip()  # 无文本页返回 None，空串兜底后跳过
                extraction_metadata: dict[str, Any] = {}
                if not content and ocr_provider is not None:
                    ocr_text = _ocr_page_text(page_number - 1)
                    if ocr_text.strip():
                        content = ocr_text.strip()
                        # 低置信标记：OCR 文本的公式/排版还原度低于原生文本层，
                        # 检索侧与评价侧据此辨别内容来源。
                        extraction_metadata = {"extraction": "ocr"}
                if extractor is not None:
                    tables = extractor.page_tables_markdown(page_number)
                    if tables:
                        content = f"{content}\n\n{tables}" if content else tables
                if content:
                    found_nonempty = True
                    yield KnowledgeDocument(
                        document_id=resolved_id,
                        content=content,
                        source=public_source,
                        page=page_number,
                        metadata=extraction_metadata,
                    )
        except Exception as exc:
            raise ValueError(f"Cannot extract text from PDF '{source.name}': {exc}") from exc

        # 整份 PDF 一页可提取文本都没有，直接报错避免静默入库空文档。
        # 分段模式下补一句范围提示：范围选错（如全书无该页）也会走到这里。
        if not found_nonempty:
            hint = (
                f"（page_range={page_range!r} 内无有效页，请检查分段范围）"
                if page_range is not None
                else ""
            )
            raise ValueError(
                f"PDF '{source.name}' contains no extractable text{hint}"
            )
    finally:
        if extractor is not None:
            extractor.close()
        if pdfium_doc is not None:
            pdfium_doc.close()


def _public_source(source: Path, source_label: str | None) -> str:
    """Keep private file paths out of knowledge models and public results."""
    if source_label is None:
        return source.name
    return source_label


def _default_document_id(source: Path) -> str:
    """Derive a stable ID without embedding the absolute path in the ID itself."""
    # 用路径哈希做稳定 ID：同一路径每次生成的 ID 一致，且不暴露本地路径。
    normalized_path = os.path.normcase(str(source.resolve()))
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    return f"{source.stem}:{digest}"
