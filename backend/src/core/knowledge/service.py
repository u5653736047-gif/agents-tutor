"""Application service that connects document chunking with an index."""

from __future__ import annotations

from collections.abc import Iterable

from .chunking import chunk_documents, chunk_documents_semantic
from .index import KnowledgeIndex
from .models import KnowledgeChunk, KnowledgeDocument, SearchHit
from .retrieval import QueryRewriter, multi_query_search

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
        rewriter: QueryRewriter | None = None,
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
        - rewriter（S4-T1）：查询改写器，可选。None 表示默认
          IdentityQueryRewriter——不改写，检索行为与 S3 完全一致
          （零回归）；传入自定义改写器后，每次 search 会把 query
          改写为多个变体、每变体各检索一次、按 chunk_id 去重后以
          max 分数合并排序（协议与语义详见 retrieval.py 模块注释；
          改写失败自动降级为原始 query 单路，不抛错）。
        """
        if chunking not in _CHUNKING_STRATEGIES:
            raise ValueError("chunking must be 'character' or 'semantic'")
        self._index = index
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._chunking = chunking
        self._max_chunk_size = max_chunk_size
        self._min_chunk_size = min_chunk_size
        self._rewriter = rewriter

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

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """Validate public search inputs, then delegate ranking to the index.

        S4-T1 多路检索编排（面向初学者）：search 现在走 retrieval.py
        的 multi_query_search——默认不改写（Identity 零回归，结果与
        S3 逐项一致）；注入改写器后，query 会被改写为多个变体、每个
        变体各检索一次、按 chunk_id 去重后以 max 分数合并排序；改写
        失败自动降级为原始 query 单路检索，不抛错（语义详见
        retrieval.py 模块注释第 3/4/5 节）。

        过滤语义（S3-T3，面向初学者）：metadata_filter 是「键 → 值」
        字典，例如 {"source": "ml-zhouzhihua", "difficulty": "intermediate"}
        表示「只在这本书、这个难度里检索」。规则：
        - 多键之间是「并且」关系，全部满足才入选；
        - 键 "source" 限定逻辑来源（某本书）；其余键匹配 chunk 的
          领域字段 subject/difficulty/chapter/section/tags（字段约定
          见 models.py 模块注释）；
        - 过滤在打分排序之前生效（索引层实现），top_k 截断发生在
          过滤之后——过滤后不足 top_k 个就返回全部匹配；
        - 没有任何匹配时返回空列表（不报错）；
        - 多路检索下过滤条件透传给每一个变体，被过滤的 chunk 不会
          进入任何变体、自然也不进合并结果（与单路语义一致）。
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k must be between 1 and 10")
        return multi_query_search(
            self._index,
            query,
            top_k,
            rewriter=self._rewriter,
            metadata_filter=metadata_filter,
        )


__all__ = ["KnowledgeService"]
