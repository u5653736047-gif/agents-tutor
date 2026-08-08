"""Load local text and PDF files into knowledge documents."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .models import KnowledgeDocument


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
) -> Iterator[KnowledgeDocument]:
    """惰性逐页解析 PDF，产出每个非空页对应的 KnowledgeDocument。

    与 load_pdf 语义一致（document_id / source 映射、空页跳过、错误消息），
    差别在于：
    1. 逐页 yield，不一次性把所有页文本留在内存，适合 190MB 级别的大文件；
    2. 支持 progress(page, total) 进度回调，批量入库脚本用它打印解析进度；
    3. 解析/提取异常在迭代过程中抛出（同样包装为含文件名的 ValueError）。
    """
    # 惰性导入：纯文本加载路径不依赖 pypdf。
    from pypdf import PdfReader

    source = Path(path)
    public_source = _public_source(source, source_label)
    try:
        reader = PdfReader(source)
    except Exception as exc:
        raise ValueError(f"Cannot read PDF '{source.name}': {exc}") from exc

    # 整份 PDF 的所有页共用一个 document_id，页与页之间靠 page 字段区分。
    resolved_id = document_id if document_id is not None else _default_document_id(source)
    total_pages = len(reader.pages)
    found_nonempty = False
    try:
        for page_number, page in enumerate(reader.pages, start=1):  # 页码从 1 开始
            if progress is not None:
                progress(page_number, total_pages)
            content = (page.extract_text() or "").strip()  # 无文本页返回 None，空串兜底后跳过
            if content:
                found_nonempty = True
                yield KnowledgeDocument(
                    document_id=resolved_id,
                    content=content,
                    source=public_source,
                    page=page_number,
                )
    except Exception as exc:
        raise ValueError(f"Cannot extract text from PDF '{source.name}': {exc}") from exc

    # 整份 PDF 一页可提取文本都没有，直接报错避免静默入库空文档。
    if not found_nonempty:
        raise ValueError(f"PDF '{source.name}' contains no extractable text")


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
