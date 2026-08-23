"""Tests for the knowledge ingestion and retrieval service."""

from __future__ import annotations

from typing import Any

import pytest

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import (
    Citation,
    KnowledgeChunk,
    KnowledgeDocument,
    SearchHit,
)
from core.knowledge.service import KnowledgeService


def _assert_chunk_coordinates(
    chunks: list[KnowledgeChunk],
    documents: list[KnowledgeDocument],
) -> None:
    source_documents = {
        (document.document_id, document.page): document for document in documents
    }
    for chunk in chunks:
        document = source_documents[(chunk.document_id, chunk.page)]
        assert 0 <= chunk.start < chunk.end <= len(document.content)
        assert chunk.content == document.content[chunk.start : chunk.end]
        page = chunk.page if chunk.page is not None else 0
        assert chunk.chunk_id == (
            f"{chunk.document_id}:{page}:{chunk.start}:{chunk.end}"
        )


def test_service_imports_searches_and_deletes_inline_documents() -> None:
    service = KnowledgeService(
        InMemoryKnowledgeIndex(),
        chunk_size=30,
        overlap=5,
    )
    document = KnowledgeDocument(
        document_id="physics",
        content="Newton explained how force changes motion.",
        source="inline:physics",
    )

    chunks = service.add_documents([document])
    hits = service.search("force motion", top_k=5)

    assert chunks
    assert hits
    _assert_chunk_coordinates(chunks, [document])
    assert hits[0].chunk.document_id == "physics"
    assert hits[0].citation.source == "inline:physics"

    service.delete_document("physics")
    assert service.search("force motion", top_k=5) == []


def test_delete_document_removes_all_pages_and_preserves_other_documents() -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=20, overlap=3)
    documents = [
        KnowledgeDocument(
            document_id="guide",
            content="algebra foundations and equations",
            source="guide.pdf",
            page=1,
        ),
        KnowledgeDocument(
            document_id="guide",
            content="geometry foundations and proofs",
            source="guide.pdf",
            page=2,
        ),
        KnowledgeDocument(
            document_id="control",
            content="control material survives deletion",
            source="control.txt",
        ),
    ]
    chunks = service.add_documents(documents)

    _assert_chunk_coordinates(chunks, documents)
    service.delete_document("guide")

    assert service.search("algebra", top_k=5) == []
    assert service.search("geometry", top_k=5) == []
    assert service.search("control", top_k=5)[0].chunk.document_id == "control"


def test_reimport_replaces_stale_chunks_for_the_same_document() -> None:
    service = KnowledgeService(
        InMemoryKnowledgeIndex(),
        chunk_size=20,
        overlap=3,
    )
    original_documents = [
        KnowledgeDocument(
            document_id="lesson",
            content="legacy algebra " + "padding " * 8 + "obsolete",
            source="lesson.pdf",
            page=1,
        ),
        KnowledgeDocument(
            document_id="lesson",
            content="legacy geometry " + "padding " * 8 + "deprecated",
            source="lesson.pdf",
            page=2,
        ),
    ]
    replacement_documents = [
        KnowledgeDocument(
            document_id="lesson",
            content="fresh calculus",
            source="lesson.pdf",
            page=1,
        ),
        KnowledgeDocument(
            document_id="lesson",
            content="fresh statistics",
            source="lesson.pdf",
            page=2,
        ),
    ]
    service.add_documents(original_documents)

    replacement_chunks = service.add_documents(replacement_documents)
    calculus_hits = service.search("calculus", top_k=5)
    statistics_hits = service.search("statistics", top_k=5)

    assert service.search("obsolete", top_k=5) == []
    assert service.search("deprecated", top_k=5) == []
    assert {hit.citation.page for hit in calculus_hits} == {1}
    assert {hit.citation.page for hit in statistics_hits} == {2}
    _assert_chunk_coordinates(replacement_chunks, replacement_documents)
    _assert_chunk_coordinates(
        [hit.chunk for hit in calculus_hits + statistics_hits],
        replacement_documents,
    )


def test_identical_reingest_is_idempotent_and_keeps_coordinates() -> None:
    service = KnowledgeService(
        InMemoryKnowledgeIndex(),
        chunk_size=100,
        overlap=10,
    )
    documents = [
        KnowledgeDocument(
            document_id="optimization",
            content="gradient descent learning rate",
            source="optimization.pdf",
            page=1,
        ),
        KnowledgeDocument(
            document_id="optimization",
            content="matrix eigenvalue decomposition",
            source="optimization.pdf",
            page=2,
        ),
    ]

    first_chunks = service.add_documents(documents)
    first_hits = service.search("gradient matrix", top_k=10)
    second_chunks = service.add_documents(documents)
    second_hits = service.search("gradient matrix", top_k=10)

    assert second_chunks == first_chunks
    assert second_hits == first_hits
    _assert_chunk_coordinates(second_chunks, documents)
    _assert_chunk_coordinates([hit.chunk for hit in second_hits], documents)


@pytest.mark.parametrize("page", [None, 1])
def test_duplicate_document_page_is_rejected_before_replacement(
    page: int | None,
) -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex())
    original = KnowledgeDocument(
        document_id="guide",
        content="original material",
        source="guide.pdf",
        page=page,
    )
    service.add_documents([original])
    duplicate_page = [
        KnowledgeDocument(
            document_id="guide",
            content="first replacement",
            source="guide.pdf",
            page=page,
        ),
        KnowledgeDocument(
            document_id="guide",
            content="second replacement",
            source="guide.pdf",
            page=page,
        ),
    ]

    with pytest.raises(ValueError, match="duplicate document page"):
        service.add_documents(duplicate_page)

    assert service.search("original", top_k=5)[0].chunk.content == original.content


def test_one_batch_keeps_all_pages_with_the_same_document_id() -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=20, overlap=0)
    documents = [
        KnowledgeDocument(
            document_id="guide",
            content="algebra",
            source="guide.pdf",
            page=1,
        ),
        KnowledgeDocument(
            document_id="guide",
            content="geometry",
            source="guide.pdf",
            page=2,
        ),
    ]

    first_chunks = service.add_documents(documents)
    second_chunks = service.add_documents(documents)

    assert second_chunks == first_chunks
    _assert_chunk_coordinates(second_chunks, documents)
    assert service.search("algebra", top_k=5)[0].citation.page == 1
    assert service.search("geometry", top_k=5)[0].citation.page == 2


@pytest.mark.parametrize("query", ["", "   "])
def test_service_rejects_empty_queries(query: str) -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex())

    with pytest.raises(ValueError, match="query"):
        service.search(query, top_k=5)


@pytest.mark.parametrize("top_k", [0, -1, 11])
def test_service_rejects_top_k_outside_supported_range(top_k: int) -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex())

    with pytest.raises(ValueError, match="top_k"):
        service.search("question", top_k=top_k)


# ── S5-C1 决策 4/5：命名空间两腿合并与阈值单调性 ───────────────────


def _ns_chunk(
    chunk_id: str,
    content: str,
    namespace: str,
    *,
    document_id: str | None = None,
) -> KnowledgeChunk:
    doc_id = document_id if document_id is not None else chunk_id
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        content=content,
        source=f"{doc_id}.txt",
        page=None,
        start=0,
        end=len(content),
        metadata={"namespace": namespace},
    )


def test_namespace_search_merges_bound_space_with_public() -> None:
    """绑定空间 X：命中 X ∪ public，不含第三空间；top_k 截断生效。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _ns_chunk("x-strong", "梯度下降算法详解", "x"),
            _ns_chunk("pub-strong", "梯度下降算法原理", "public"),
            _ns_chunk("third-hit", "梯度下降入门", "third"),
        ]
    )
    service = KnowledgeService(index)

    hits = service.search("梯度下降", top_k=2, namespace="x")

    hit_ids = [hit.chunk.chunk_id for hit in hits]
    assert len(hit_ids) == 2  # top_k 截断（三腿候选共 3 个）
    assert set(hit_ids) == {"x-strong", "pub-strong"}
    assert "third-hit" not in hit_ids


def test_merge_namespace_legs_deduplicates_taking_max_score() -> None:
    """同 chunk_id 两腿重复 → 取高分；平局按 chunk_id 升序。"""
    from core.knowledge.service import KnowledgeService

    def hit(chunk_id: str, score: float) -> Any:
        return SearchHit(
            chunk=_ns_chunk(chunk_id, "内容", "public"),
            citation=Citation(
                document_id="doc",
                source="doc.txt",
                page=None,
                chunk_id=chunk_id,
            ),
            score=score,
        )

    legs = [
        [hit("shared", 1.0), hit("aaa", 2.0)],
        [hit("shared", 3.0)],
    ]

    merged = KnowledgeService._merge_namespace_legs(legs, top_k=10)

    assert [item.chunk.chunk_id for item in merged] == ["shared", "aaa"]
    scores = {item.chunk.chunk_id: item.score for item in merged}
    assert scores["shared"] == 3.0  # 取最大分


# ── S5-C1 决策 5：阈值单调性与 embedding 复用 ─────────────────────


def test_threshold_monotonicity_weak_bound_strong_public_met() -> None:
    """本空间弱命中 + 公共库强命中 → 合并 top_score 由公共库抬升 → 达标。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            # 本空间弱命中：只含一个查询词项「梯」。
            _ns_chunk("x-weak", "梯", "x"),
            # 公共库强命中：多个查询词项。
            _ns_chunk("pub-strong", "梯度下降算法优化", "public"),
        ]
    )
    service = KnowledgeService(index)

    result = service.adaptive_search(
        "梯度下降算法优化",
        top_k=5,
        namespace="x",
        relevance_threshold=2.0,
    )

    assert result.metadata.threshold_met is True
    hit_ids = [hit.chunk.chunk_id for hit in result.hits]
    assert set(hit_ids) == {"x-weak", "pub-strong"}


def test_threshold_monotonicity_no_hits_anywhere_not_met() -> None:
    """双空间均无命中 → 未达标（且不抛错）。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _ns_chunk("x-unrelated", "完全无关的内容", "x"),
            _ns_chunk("pub-unrelated", "风马牛不相及", "public"),
        ]
    )
    service = KnowledgeService(index)

    result = service.adaptive_search(
        "梯度下降算法优化",
        top_k=5,
        namespace="x",
        relevance_threshold=1.0,
    )

    assert result.hits == []
    assert result.metadata.threshold_met is False


def test_threshold_disabled_uses_neutral_stop_reason() -> None:
    """未启用阈值时合并分支的中性 stopped_reason（小缺陷清理锁定）。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_ns_chunk("pub-only", "梯度下降", "public")])
    service = KnowledgeService(index)

    # 用非 public 空间触发两腿合并分支（单路路径的 stopped_reason 来自
    # retrieval 层原生文案，与本修复无关）。
    result = service.adaptive_search("梯度下降", top_k=5, namespace="x")

    assert result.metadata.threshold is None
    assert result.metadata.stopped_reason == "命名空间并集合并（未启用阈值判定）"


class _CountingEmbedProvider:
    """embed 调用计数替身：验证两腿合并检索复用同一次 query embedding。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0, 0.0, 0.0] for text in texts]


class _CountingHybridHarness:
    """计数 provider + 混合索引：验证两腿合并检索复用同一次 embedding。"""

    def __init__(self) -> None:
        from core.knowledge.hybrid import HybridKnowledgeIndex
        from core.knowledge.vector_index import InMemoryVectorKnowledgeIndex

        self.provider = _CountingEmbedProvider()
        self.lexical = InMemoryKnowledgeIndex()
        self.vector = InMemoryVectorKnowledgeIndex(self.provider)
        self.index = HybridKnowledgeIndex(self.lexical, self.vector)

    def seed(self, chunks: list[KnowledgeChunk]) -> None:
        # 分别写入词法与向量两路：混合索引检索时两路都有数据。
        for chunk in chunks:
            self.vector.upsert([chunk])
            self.lexical.upsert([chunk])

    def query_texts_seen(self) -> list[str]:
        texts: list[str] = []
        for call in self.provider.calls:
            texts.extend(call)
        return texts


def test_embed_reused_once_across_namespace_legs_inmemory() -> None:
    """两腿相同 query → 向量索引 LRU 缓存命中，只 embed 一次。"""
    from core.knowledge.hybrid import HybridKnowledgeIndex
    from core.knowledge.vector_index import InMemoryVectorKnowledgeIndex

    provider = _CountingEmbedProvider()
    lexical = InMemoryKnowledgeIndex()
    vector = InMemoryVectorKnowledgeIndex(provider)
    service = KnowledgeService(HybridKnowledgeIndex(lexical, vector))
    lexical.upsert(
        [
            _ns_chunk("x-weak", "梯", "x"),
            _ns_chunk("pub-strong", "梯度下降算法的原理与优化技巧", "public"),
        ]
    )
    vector.upsert(
        [
            _ns_chunk("x-weak", "梯", "x"),
            _ns_chunk("pub-strong", "梯度下降算法的原理与优化技巧", "public"),
        ]
    )

    hits = service.search("梯度下降算法优化", top_k=5, namespace="x")

    assert {hit.chunk.chunk_id for hit in hits} == {"x-weak", "pub-strong"}
    query_texts = [text for call in provider.calls for text in call]
    assert query_texts.count("梯度下降算法优化") == 1


def test_embed_reused_once_across_namespace_legs_sqlite(tmp_path) -> None:
    """SqliteVectorKnowledgeIndex 同样复用：_query_vector LRU 按锁保护。"""
    from core.knowledge.hybrid import HybridKnowledgeIndex
    from core.knowledge.vector_index import SqliteVectorKnowledgeIndex

    provider = _CountingEmbedProvider()
    lexical = InMemoryKnowledgeIndex()
    vector = SqliteVectorKnowledgeIndex(tmp_path / "v.db", provider)
    service = KnowledgeService(HybridKnowledgeIndex(lexical, vector))
    # 注意：pub-strong 的内容刻意与查询文本不同——upsert 会 embed 内容，
    # 若内容与查询同串，计数断言会把「内容 embedding」误计入「查询复用」。
    chunks = [
        _ns_chunk("x-weak", "梯", "x"),
        _ns_chunk("pub-strong", "梯度下降法很有效", "public"),
    ]
    for chunk in chunks:
        vector.upsert([chunk])
        lexical.upsert([chunk])

    hits = service.search("梯度下降算法优化", top_k=5, namespace="x")

    assert {hit.chunk.chunk_id for hit in hits} == {"x-weak", "pub-strong"}
    query_texts = [text for call in provider.calls for text in call]
    assert query_texts.count("梯度下降算法优化") == 1


def test_merged_branch_policy_no_retrieval_aligns_with_single_path() -> None:
    """修复 3 回归：合并分支 policy 判定「无需检索」时与单路语义一致——
    threshold_met=None + 中性「无需检索」reason，不再错误暗示「未达阈值」。
    """

    class _NoRetrievePolicy:
        def needs_retrieval(self, query: str) -> object:
            from core.knowledge.policy import RetrievalDecision

            return RetrievalDecision(retrieve=False, reason="寒暄类问题")

    index = InMemoryKnowledgeIndex()
    index.upsert([_ns_chunk("x-strong", "梯度下降算法详解", "x")])
    service = KnowledgeService(index)

    result = service.adaptive_search(
        "你好呀", top_k=5, namespace="x", policy=_NoRetrievePolicy()
    )

    assert result.hits == []
    assert result.metadata.needed is False
    assert result.metadata.threshold_met is None
    assert result.metadata.stopped_reason.startswith("无需检索：")
