"""Application service that connects document chunking with an index."""

from __future__ import annotations

from collections.abc import Iterable

from .chunking import chunk_documents
from .index import KnowledgeIndex
from .models import KnowledgeChunk, KnowledgeDocument, SearchHit


class KnowledgeService:
    """Provide the small write, delete, and search API used by agent tools."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        chunk_size: int = 1000,
        overlap: int = 100,
    ) -> None:
        self._index = index
        self._chunk_size = chunk_size
        self._overlap = overlap

    def add_documents(self, documents: Iterable[KnowledgeDocument]) -> list[KnowledgeChunk]:
        """Replace the supplied documents, then return their stored chunks."""
        document_batch = list(documents)
        chunks = chunk_documents(
            document_batch,
            chunk_size=self._chunk_size,
            overlap=self._overlap,
        )
        # 同一 PDF 的多页共用 document_id，因此先统一清理，再写入整批分块。
        document_ids = dict.fromkeys(
            document.document_id for document in document_batch
        )
        for document_id in document_ids:
            self._index.delete_document(document_id)
        self._index.upsert(chunks)
        return chunks

    def delete_document(self, document_id: str) -> None:
        """Remove all chunks for a document."""
        self._index.delete_document(document_id)

    def search(self, query: str, top_k: int = 5) -> list[SearchHit]:
        """Validate public search inputs, then delegate ranking to the index."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k must be between 1 and 10")
        return self._index.search(query, top_k)


__all__ = ["KnowledgeService"]
