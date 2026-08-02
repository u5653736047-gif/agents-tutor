"""Load local text and PDF files into knowledge documents."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .models import KnowledgeDocument


def load_text(
    path: str | Path,
    *,
    document_id: str | None = None,
) -> list[KnowledgeDocument]:
    """Load one non-empty UTF-8 text file."""
    source = Path(path)
    content = source.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Text file '{source.name}' is empty")

    return [
        KnowledgeDocument(
            document_id=document_id if document_id is not None else _default_document_id(source),
            content=content,
            source=str(source),
        )
    ]


def load_pdf(
    path: str | Path,
    *,
    document_id: str | None = None,
) -> list[KnowledgeDocument]:
    """Load each non-empty PDF page as a separate document."""
    # Import lazily so plain-text loading does not require the PDF dependency.
    from pypdf import PdfReader

    source = Path(path)
    try:
        reader = PdfReader(source)
    except Exception as exc:
        raise ValueError(f"Cannot read PDF '{source.name}': {exc}") from exc

    resolved_id = document_id if document_id is not None else _default_document_id(source)
    documents: list[KnowledgeDocument] = []
    try:
        for page_number, page in enumerate(reader.pages, start=1):
            content = (page.extract_text() or "").strip()
            if content:
                documents.append(
                    KnowledgeDocument(
                        document_id=resolved_id,
                        content=content,
                        source=str(source),
                        page=page_number,
                    )
                )
    except Exception as exc:
        raise ValueError(f"Cannot extract text from PDF '{source.name}': {exc}") from exc

    if not documents:
        raise ValueError(f"PDF '{source.name}' contains no extractable text")
    return documents


def _default_document_id(source: Path) -> str:
    """Derive a stable ID without embedding the absolute path in the ID itself."""
    normalized_path = os.path.normcase(str(source.resolve()))
    digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()
    return f"{source.stem}:{digest}"
