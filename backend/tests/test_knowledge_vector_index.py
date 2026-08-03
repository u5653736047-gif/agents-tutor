"""S3-T4 向量索引测试：检索正确性、持久化重载、过滤组合、语义命中、ingest 接入。

覆盖清单 A S3-T4 验收标准对应项：
1. 向量索引检索正确性：余弦相似度排序、top_k、upsert 覆盖、整文档删除
   （用固定向量替身 provider 精确断言，不依赖哈希细节）；
2. 持久化重载：写 → close → 重开 → 检索（SQLite BLOB 往返 + metadata 往返）；
3. 与 metadata_filter 组合：复用 S3-T3 语义（先过滤后排序，空结果返回 []）；
4. 语义命中用例：构造「同义近邻」替身（normalize 同义词归一化），
   证明向量检索可命中词法索引无法命中的同义表述（土豆/马铃薯、
   CNN/卷积神经网络，均带词法对照断言）；
5. 与现有系统衔接：KnowledgeService 直接挂向量索引（协议替换点）、
   ingest_book 的 vector_index 同步与增量补建（chunks_of_document）；
6. 不依赖外部网络：全部用例用 HashEmbeddingProvider 或固定向量替身。

测试替身设计（面向初学者）：EmbeddingProvider 是可替换协议，测试
注入两种替身——固定向量替身（手工指定文本→向量，精确验证相似度
计算）与同义词归一化替身（模拟语义模型能力，验证检索链路本身）。
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ingest_books import ManifestBook, VerifyCase, ingest_book

from core.knowledge.embedding import HashEmbeddingProvider
from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import (
    InMemoryVectorKnowledgeIndex,
    SqliteVectorKnowledgeIndex,
    _dot,
    _unpack_vector,
)

# ── 小工具与测试替身 ──────────────────────────────────────────────


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
    metadata: dict[str, Any] | None = None,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=f"{document_id}.txt",
        page=None,
        start=0,
        end=len(content),
        metadata=metadata or {},
    )


class _FixedVectorProvider:
    """测试替身：文本 → 手工指定向量（维度 3，故意不归一化）。

    用途（面向初学者）：把「embedding 质量」从「索引检索正确性」中
    剥离——向量是测试写死的，索引的归一化、点积、排序行为就能被
    精确断言，不依赖哈希实现细节。
    """

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.dimension = 3
        self._mapping = mapping

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._mapping.get(text, [0.0, 0.0, 0.0]) for text in texts]


def _synonym_provider() -> HashEmbeddingProvider:
    """测试替身：同义词归一化哈希向量（模拟语义模型的等价映射能力）。

    归一化把别名替换成规范形后再做字符特征哈希：查询「土豆」与
    分块「马铃薯…」经归一化后共享特征 → 相似度 > 0，而词法索引
    看不到这层关系。真实语义效果由 FastEmbedProvider（bge-small-zh
    离线模型）提供，验证方式见 docs/EMBEDDING_SELECTION.md。
    """
    synonyms = {"土豆": "马铃薯", "cnn": "卷积神经网络"}

    def normalize(text: str) -> str:
        lowered = text.lower()
        for alias, canonical in synonyms.items():
            lowered = lowered.replace(alias.lower(), canonical)
        return lowered

    return HashEmbeddingProvider(normalize=normalize)


def _manifest_book(source: str) -> ManifestBook:
    """构造内存中的书条目（不落盘，配合假 page_loader 使用）。"""
    return ManifestBook(
        source=source,
        file="book.pdf",
        title=f"《{source}》",
        authors=["测试作者"],
        subjects=["机器学习"],
        difficulty="beginner",
        blocked=None,
        verify=[VerifyCase(query="支持向量机", expected_source=source)],
    )


def _pages_with(texts: list[str]) -> Any:
    """构造假 page_loader：按给定文本列表逐页产出 KnowledgeDocument。"""

    def load(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        for page_number, text in enumerate(texts, start=1):
            yield KnowledgeDocument(
                document_id=document_id,
                content=text,
                source=source_label,
                page=page_number,
            )

    return load


# ── 1. 检索正确性（固定向量替身精确断言）─────────────────────────


def test_vector_index_ranks_by_cosine_similarity() -> None:
    """余弦排序：查询向量与哪个分块方向最一致，哪个排最前。

    向量故意不归一化（[3,0,0] 长度 3），验证索引层负责归一化：
    归一化后余弦 = 点积，分数恰为 1.0 与 0.707…。
    """
    provider = _FixedVectorProvider(
        {
            "alpha": [1.0, 0.0, 0.0],
            "beta": [0.0, 1.0, 0.0],
            "gamma": [0.5, 0.5, 0.0],  # 与 alpha 的余弦 = 0.707…
        }
    )
    index = InMemoryVectorKnowledgeIndex(provider)
    index.upsert(
        [
            _chunk("beta", "beta"),
            _chunk("gamma", "gamma"),
            _chunk("alpha", "alpha"),
        ]
    )

    hits = index.search("alpha", top_k=3)

    assert [hit.chunk.chunk_id for hit in hits] == ["alpha", "gamma"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.5 / (0.5**2 + 0.5**2) ** 0.5)
    assert hits[0].score > hits[1].score > 0
    # 与 alpha 正交的 beta：余弦 0 → 不相关跳过。
    assert all(hit.chunk.chunk_id != "beta" for hit in hits)


def test_vector_index_upsert_replaces_and_delete_document() -> None:
    """同 chunk_id 覆盖、整文档删除（与词法索引语义对齐）。"""
    provider = _FixedVectorProvider(
        {
            "old vocabulary": [1.0, 0.0, 0.0],
            # 新内容与旧内容向量正交：替换后旧查询余弦为 0，不再命中。
            "new concept": [0.0, 1.0, 0.0],
            "shared term": [1.0, 0.0, 0.0],
        }
    )
    index = InMemoryVectorKnowledgeIndex(provider)
    index.upsert([_chunk("shared", "old vocabulary", document_id="old-doc")])

    index.upsert([_chunk("shared", "new concept", document_id="new-doc")])
    assert [hit.chunk.chunk_id for hit in index.search("new concept", top_k=5)] == [
        "shared"
    ]
    assert index.search("old vocabulary", top_k=5) == []

    index.upsert([_chunk("second", "shared term", document_id="doc-2")])
    index.delete_document("new-doc")
    assert [hit.chunk.chunk_id for hit in index.search("shared term", top_k=5)] == [
        "second"
    ]


def test_vector_index_empty_query_or_index_returns_empty() -> None:
    provider = HashEmbeddingProvider()
    index = InMemoryVectorKnowledgeIndex(provider)

    assert index.search("anything", top_k=5) == []
    index.upsert([_chunk("c1", "马铃薯是重要的粮食作物")])
    assert index.search("", top_k=5) == []
    assert index.search("   ", top_k=5) == []
    assert index.search("土豆", top_k=0) == []


def test_vector_index_rejects_inconsistent_provider_output() -> None:
    """替身输出数量/维度与契约不符时尽早报错（防错误相似度入库）。"""

    class _BrokenProvider:
        dimension = 3

        def embed(self, texts: list[str]) -> list[list[float]]:
            # 故意返回 2 维向量（与声明的 3 维不符）。
            return [[0.0, 0.0] for _ in texts]

    index = InMemoryVectorKnowledgeIndex(_BrokenProvider())
    with pytest.raises(ValueError, match="dimension"):
        index.upsert([_chunk("c1", "任何文本")])


def test_vector_index_rejects_wrong_vector_count() -> None:
    """替身返回的向量数量与 chunk 数量不一致时尽早报错（M-4 防御分支）。

    upsert 同时写入多个 chunk 时，provider 必须为每个 chunk 返回一个
    向量；数量对不上说明实现有 bug，直接抛错而不是静默丢数据。
    """

    class _WrongCountProvider:
        dimension = 3

        def embed(self, texts: list[str]) -> list[list[float]]:
            # 无论输入多少文本都只返回 1 个向量（数量对不上）。
            return [[0.0, 0.0, 0.0]]

    index = InMemoryVectorKnowledgeIndex(_WrongCountProvider())
    with pytest.raises(ValueError, match="数量"):
        index.upsert([_chunk("c1", "文本一"), _chunk("c2", "文本二")])


def test_unpack_vector_rejects_corrupt_blob() -> None:
    """BLOB 长度不是 float32 的整数倍 → 明确报错（M-4 数据损坏检测）。"""
    with pytest.raises(ValueError, match="float32"):
        _unpack_vector(b"\x00\x00\x00")


def test_dot_rejects_mismatched_lengths() -> None:
    """_dot 对长度不一致的向量直接抛错（I-1 防御：不静默截断）。

    zip 遇到长度不同的列表会静默截断到较短长度，导致错误相似度；
    长度断言把「换过不同维度 provider 却没重建向量库」这类配置错误
    暴露出来，而不是悄悄算错。
    """
    with pytest.raises(ValueError, match="长度"):
        _dot([1.0, 2.0, 3.0], [1.0, 2.0])


# ── 2. 持久化重载（写 → 关 → 开 → 检索）──────────────────────────


def test_sqlite_vector_index_persists_after_reopen(tmp_path: Path) -> None:
    """关闭后重开同一数据库文件：向量与 chunk 字段都能恢复并检索。"""
    db_path = tmp_path / "vector.db"
    provider = HashEmbeddingProvider()
    first = SqliteVectorKnowledgeIndex(db_path, provider=provider)
    first.upsert(
        [
            _chunk(
                "c1",
                "马铃薯是重要的粮食作物",
                document_id="agri",
                metadata={"subject": "农业", "difficulty": "beginner"},
            )
        ]
    )
    first.close()

    second = SqliteVectorKnowledgeIndex(db_path, provider=provider)
    try:
        hits = second.search("马铃薯", top_k=5)
        assert [hit.chunk.chunk_id for hit in hits] == ["c1"]
        assert hits[0].citation.document_id == "agri"
        assert hits[0].chunk.metadata == {
            "subject": "农业",
            "difficulty": "beginner",
        }
        # 重载后仍支持过滤与删除（内存矩阵与 SQLite 数据一致）。
        assert (
            second.search(
                "马铃薯",
                top_k=5,
                metadata_filter={"difficulty": "advanced"},
            )
            == []
        )
        assert second.has_document("agri")
        second.delete_document("agri")
        assert second.search("马铃薯", top_k=5) == []
    finally:
        second.close()


def test_sqlite_vector_index_rejects_dimension_mismatch_on_reload(
    tmp_path: Path,
) -> None:
    """换维度不同的 provider 重载旧库 → 加载时明确报错（I-1 核心场景）。

    场景（面向初学者）：先用 256 维的哈希替身入库，再换成 512 维的
    fastembed 重开同一个向量库——旧库向量与查询向量维度不一致，如果
    不拦截会被 zip 静默截断算出错误相似度。正确行为是加载时直接抛错，
    提示用 --force 重新入库重建向量库（ingest 脚本的 --force 会先删
    旧向量再写新向量，见 ingest_books.py）。
    """
    db_path = tmp_path / "vector.db"
    first = SqliteVectorKnowledgeIndex(db_path, provider=HashEmbeddingProvider())
    first.upsert([_chunk("c1", "马铃薯是重要的粮食作物")])
    first.close()

    with pytest.raises(ValueError, match="--force"):
        SqliteVectorKnowledgeIndex(
            db_path, provider=HashEmbeddingProvider(dimension=512)
        )


# ── 3. metadata_filter 组合（复用 S3-T3 语义）────────────────────


def test_vector_index_metadata_filter_composes() -> None:
    """过滤先于打分排序：限书/限难度后再排序；过滤后空结果返回 []。"""
    provider = _FixedVectorProvider(
        {
            "support vector machine kernel margin": [1.0, 0.0, 0.0],
            "support vector machine": [0.9, 0.1, 0.0],
            # 与查询方向有一点夹角：余弦 > 0，可参与排序但排最后。
            "attention mechanism": [0.2, 0.8, 0.0],
        }
    )
    chunks = [
        _chunk(
            "c1",
            "support vector machine kernel margin",
            metadata={
                "subject": "机器学习",
                "difficulty": "advanced",
                "tags": ["支持向量机"],
            },
        ),
        _chunk(
            "c2",
            "support vector machine",
            metadata={"subject": "机器学习", "difficulty": "intermediate"},
        ),
        _chunk(
            "c3",
            "attention mechanism",
            document_id="doc-2",
            metadata={"subject": "深度学习", "difficulty": "intermediate"},
        ),
    ]
    index = InMemoryVectorKnowledgeIndex(provider)
    index.upsert(chunks)

    query = "support vector machine kernel margin"

    # 不过滤：按余弦降序 c1 > c2 > c3。
    assert [
        hit.chunk.chunk_id
        for hit in index.search(query, top_k=3)
    ] == ["c1", "c2", "c3"]
    # 过滤 intermediate 后：c1 被剔除，top1 变 c2（过滤先于排序）。
    hits = index.search(
        query,
        top_k=3,
        metadata_filter={"difficulty": "intermediate"},
    )
    assert [hit.chunk.chunk_id for hit in hits] == ["c2", "c3"]
    # 多键 AND + 限书。
    hits = index.search(
        query,
        top_k=3,
        metadata_filter={"source": "doc-2.txt", "difficulty": "intermediate"},
    )
    assert [hit.chunk.chunk_id for hit in hits] == ["c3"]
    # 过滤后无匹配 → 空列表（不报错）。
    assert (
        index.search(
            query,
            top_k=3,
            metadata_filter={"difficulty": "advanced", "source": "doc-2.txt"},
        )
        == []
    )


# ── 4. 语义命中用例（词法对照：词法 0 命中，向量命中）────────────


def test_semantic_synonym_hit_where_lexical_misses_chinese() -> None:
    """「土豆」vs「马铃薯」：向量检索命中，词法检索 0 命中。"""
    chunks = [
        _chunk("potato", "马铃薯是重要的粮食作物，块茎富含淀粉", document_id="agri"),
        _chunk("corn", "玉米是重要的粮食作物，籽粒富含淀粉", document_id="agri"),
    ]
    vector_index = InMemoryVectorKnowledgeIndex(_synonym_provider())
    vector_index.upsert(chunks)

    # 词法对照：查询「土豆」与两个 chunk 无任何共享字符特征 → 0 命中。
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert(chunks)
    assert lexical.search("土豆", top_k=5) == []

    # 向量检索：归一化「土豆→马铃薯」后与 potato chunk 共享特征 → 命中。
    # 断言 top1 命中（哈希向量在 256 维下不同文本的特征桶可能小概率
    # 碰撞，其它 chunk 可能以极低分出现，但超过目标 chunk 的概率极低）。
    hits = vector_index.search("土豆", top_k=5)
    assert hits, "语义检索应命中词法无法命中的同义表述"
    assert hits[0].chunk.chunk_id == "potato"
    assert hits[0].score > 0
    # 宽松补充断言：即使极端碰撞发生，目标 chunk 也应出现在结果中。
    assert "potato" in [hit.chunk.chunk_id for hit in hits]


def test_semantic_synonym_hit_where_lexical_misses_cross_language() -> None:
    """「CNN」vs「卷积神经网络」：向量检索命中，词法检索 0 命中。"""
    chunks = [
        _chunk("cnn", "卷积神经网络在图像识别任务中表现优异", document_id="ml-dl"),
        _chunk("svm", "支持向量机适用于小样本分类任务", document_id="ml-dl"),
    ]
    vector_index = InMemoryVectorKnowledgeIndex(_synonym_provider())
    vector_index.upsert(chunks)

    # 词法对照：chunk 里既没有英文词 cnn，也没有「的」「原理」等词。
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert(chunks)
    assert lexical.search("CNN 的原理", top_k=5) == []

    # 向量检索：归一化「cnn→卷积神经网络」后与 cnn chunk 共享特征。
    # 同样断言 top1 命中（碰撞说明同上一个用例）。
    hits = vector_index.search("CNN 的原理", top_k=5)
    assert hits, "语义检索应命中词法无法命中的同义表述"
    assert hits[0].chunk.chunk_id == "cnn"
    assert hits[0].score > 0
    # 宽松补充断言：目标 chunk 应出现在结果中（概率极低的碰撞不改变此事实）。
    assert "cnn" in [hit.chunk.chunk_id for hit in hits]


# ── 5. 与现有系统衔接（KnowledgeService / ingest 脚本）───────────


def test_knowledge_service_accepts_vector_index() -> None:
    """协议替换点：KnowledgeService 构造时传入向量索引即可走向量检索。"""
    service = KnowledgeService(
        InMemoryVectorKnowledgeIndex(_synonym_provider()),
        chunk_size=30,
        overlap=5,
    )
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="agri",
                content="马铃薯是重要的粮食作物，块茎富含淀粉",
                source="agri.txt",
            )
        ]
    )

    # 词法索引查「土豆」必然 0 命中，向量索引经归一化命中——同一个
    # service.search 接口、同一个 metadata_filter 参数，行为由注入的
    # 索引决定（默认路径仍是词法索引，S3-T1 行为不变）。
    hits = service.search("土豆的种植", top_k=5)
    assert hits
    assert hits[0].chunk.document_id == "agri"
    assert hits[0].score > 0


def test_ingest_book_syncs_and_backfills_vector_index(tmp_path: Path) -> None:
    """ingest 集成：入库同步向量；已入库的书带向量重跑自动增量补建。"""
    lexical = SqliteKnowledgeIndex(tmp_path / "kb.db")
    book = _manifest_book("book-a")
    pages = _pages_with(["马铃薯是重要的粮食作物，块茎富含淀粉"])
    try:
        # 1. 不带向量入库：词法完成标记存在，向量库不存在。
        first = ingest_book(lexical, book, Path("book.pdf"), page_loader=pages)
        assert first.status == "ingested"
        assert not (tmp_path / "vector.db").exists()

        # 2. 带向量重跑：书被跳过（不重新解析），但向量增量补建成功。
        vector = SqliteVectorKnowledgeIndex(
            tmp_path / "vector.db", provider=HashEmbeddingProvider()
        )
        try:
            second = ingest_book(
                lexical,
                book,
                Path("book.pdf"),
                page_loader=pages,
                vector_index=vector,
            )
            assert second.status == "skipped"
            assert vector.has_document("book-a")
            hits = vector.search("马铃薯", top_k=5)
            assert [hit.chunk.document_id for hit in hits] == ["book-a"]
        finally:
            vector.close()

        # 3. --force 重入库：词法与向量同步整文档替换（先删后插）。
        vector = SqliteVectorKnowledgeIndex(
            tmp_path / "vector.db", provider=HashEmbeddingProvider()
        )
        try:
            third = ingest_book(
                lexical,
                book,
                Path("book.pdf"),
                page_loader=pages,
                force=True,
                vector_index=vector,
            )
            assert third.status == "ingested"
            assert vector.has_document("book-a")
        finally:
            vector.close()
    finally:
        lexical.close()
