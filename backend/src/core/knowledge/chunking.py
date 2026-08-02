"""Small deterministic character-window chunker."""

from __future__ import annotations

from collections.abc import Iterable

from .models import KnowledgeChunk, KnowledgeDocument


def _validate_window(chunk_size: int, overlap: int) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")


def chunk_document(
    document: KnowledgeDocument,
    *,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[KnowledgeChunk]:
    """Split one document into stable character windows."""
    _validate_window(chunk_size, overlap)

    chunks: list[KnowledgeChunk] = []
    start = 0
    while start < len(document.content):
        end = min(start + chunk_size, len(document.content))
        page = document.page if document.page is not None else 0
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"{document.document_id}:{page}:{start}:{end}",
                document_id=document.document_id,
                content=document.content[start:end],
                source=document.source,
                page=document.page,
                start=start,
                end=end,
                metadata=document.metadata.copy(),
            )
        )
        if end == len(document.content):
            break
        start = end - overlap
    return chunks


def chunk_documents(
    documents: Iterable[KnowledgeDocument],
    *,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[KnowledgeChunk]:
    """Chunk documents in input order."""
    _validate_window(chunk_size, overlap)
    return [
        chunk
        for document in documents
        for chunk in chunk_document(document, chunk_size=chunk_size, overlap=overlap)
    ]
