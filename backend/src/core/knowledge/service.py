"""Application service that connects document chunking with an index."""

from __future__ import annotations

from collections.abc import Iterable

from .chunking import chunk_documents, chunk_documents_semantic
from .index import KnowledgeIndex
from .models import KnowledgeChunk, KnowledgeDocument, SearchHit

# 可选分块策略（S3-T2）：
# - "character"：字符窗口分块（默认，S3-T1 起的行为，保持不变）；
# - "semantic"：按章节标题 / 段落边界分块，并保护公式与代码块不被截断。
_CHUNKING_STRATEGIES = frozenset({"character", "semantic"})


class KnowledgeService:
    """Provide the small write, delete, and search API used by agent tools."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        chunk_size: int = 1000,
        overlap: int = 100,
        chunking: str = "character",
        max_chunk_size: int = 2000,
        min_chunk_size: int = 200,
    ) -> None:
        """初始化服务。

        参数说明（面向初学者）：
        - chunking：分块策略，"character"（默认）或 "semantic"。
          "character" 走 S3-T1 的字符窗口分块，行为完全不变；
          "semantic" 按章节标题/段落边界切分（见
          chunking.chunk_document_semantic 的规则说明）。
        - chunk_size / overlap：仅 character 策略使用（窗口大小与重叠）；
        - max_chunk_size / min_chunk_size：仅 semantic 策略使用
          （目标块大小上限；超长段落切分时「最近行边界」取舍的最小值）。
        """
        if chunking not in _CHUNKING_STRATEGIES:
            raise ValueError("chunking must be 'character' or 'semantic'")
        self._index = index
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._chunking = chunking
        self._max_chunk_size = max_chunk_size
        self._min_chunk_size = min_chunk_size

    def add_documents(self, documents: Iterable[KnowledgeDocument]) -> list[KnowledgeChunk]:
        """Replace the supplied documents, then return their stored chunks."""
        document_batch = list(documents)
        coordinates: set[tuple[str, int | None]] = set()
        for document in document_batch:
            coordinate = (document.document_id, document.page)
            if coordinate in coordinates:
                raise ValueError("duplicate document page in one batch")
            coordinates.add(coordinate)

        # 按构造时选定的分块策略分块：character 保持 S3-T1 行为不变；
        # semantic 按标题/段落边界切分（公式/代码保护、坐标语义见
        # chunking 模块注释）。两种策略产出的 chunk 坐标都可回溯原文，
        # 后续的索引写入与检索链路完全复用。
        if self._chunking == "semantic":
            chunks = chunk_documents_semantic(
                document_batch,
                max_chunk_size=self._max_chunk_size,
                min_chunk_size=self._min_chunk_size,
            )
        else:
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
