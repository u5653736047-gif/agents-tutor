"""I2 chunk 读接口测试：KnowledgeIndex.chunk() + KnowledgeService.chunk()。

覆盖：
- 各索引实现（InMemory / Sqlite / Hybrid / Vector）：命中返回原分块、
  未命中返回 None；
- KnowledgeService.chunk：空白 / 超长 chunk_id 短路返回 None（不发
  起查询）、索引未实现 chunk 时兜底返回 None、正常透传；
- Hybrid 转发词法路、Vector 从内存矩阵取——与检索路径同源。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from core.knowledge.embedding import HashEmbeddingProvider
from core.knowledge.hybrid import HybridKnowledgeIndex
from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, SearchHit
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import InMemoryVectorKnowledgeIndex


def _chunk(chunk_id: str, content: str, document_id: str = "doc-1") -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=document_id,
        page=1,
        start=0,
        end=len(content),
        metadata={"title": "测试"},
    )


def test_in_memory_chunk_returns_hit_and_miss() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "支持向量机 间隔")])

    assert index.chunk("c1") is not None
    assert index.chunk("c1").chunk_id == "c1"  # type: ignore[union-attr]
    assert index.chunk("missing") is None


def test_sqlite_chunk_returns_hit_and_miss(tmp_path: Path) -> None:
    index = SqliteKnowledgeIndex(tmp_path / "knowledge.db")
    index.upsert([_chunk("c1", "支持向量机 间隔")])

    hit = index.chunk("c1")
    assert hit is not None
    assert hit.document_id == "doc-1"
    assert hit.content == "支持向量机 间隔"
    assert index.chunk("missing") is None
    index.close()


def test_sqlite_chunk_roundtrips_metadata(tmp_path: Path) -> None:
    index = SqliteKnowledgeIndex(tmp_path / "knowledge.db")
    metadata = {"title": "机器学习", "difficulty": "intermediate"}
    index.upsert([_chunk("c1", "内容", ).model_copy(update={"metadata": metadata})])

    hit = index.chunk("c1")

    assert hit is not None
    assert hit.metadata == metadata
    index.close()


def test_hybrid_chunk_forwards_to_lexical(tmp_path: Path) -> None:
    lexical = SqliteKnowledgeIndex(tmp_path / "knowledge.db")
    lexical.upsert([_chunk("c1", "支持向量机 间隔")])
    hybrid = HybridKnowledgeIndex(lexical, None)

    hit = hybrid.chunk("c1")
    assert hit is not None
    assert hit.chunk_id == "c1"
    assert hybrid.chunk("missing") is None
    hybrid.close()


def test_hybrid_chunk_with_vector_leg_prefers_lexical() -> None:
    lexical = InMemoryKnowledgeIndex()
    lexical.upsert([_chunk("c1", "支持向量机 间隔")])
    vector = InMemoryVectorKnowledgeIndex(HashEmbeddingProvider())
    vector.upsert([_chunk("c1", "支持向量机 间隔")])
    hybrid = HybridKnowledgeIndex(lexical, vector)

    hit = hybrid.chunk("c1")
    assert hit is not None
    assert hit.chunk_id == "c1"
    hybrid.close()


def test_vector_chunk_returns_hit_and_miss() -> None:
    index = InMemoryVectorKnowledgeIndex(HashEmbeddingProvider())
    index.upsert([_chunk("c1", "支持向量机 间隔")])

    assert index.chunk("c1") is not None
    assert index.chunk("missing") is None


def test_service_chunk_validates_and_forwards() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "支持向量机 间隔")])
    service = KnowledgeService(index)

    # 正常透传。
    hit = service.chunk("c1")
    assert hit is not None
    assert hit.chunk_id == "c1"
    # 空白 / 超长 chunk_id 短路返回 None（不发起查询）。
    assert service.chunk("") is None
    assert service.chunk("   ") is None
    assert service.chunk("x" * 513) is None
    # 未命中。
    assert service.chunk("missing") is None


class _SearchOnlyIndex:
    """只实现 search 的索引替身：验证 service.chunk 在协议方法缺失时兜底。"""

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        return None

    def delete_document(self, document_id: str) -> None:
        return None

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        return []


def test_service_chunk_falls_back_when_index_has_no_chunk_method() -> None:
    service = KnowledgeService(_SearchOnlyIndex())

    assert service.chunk("anything") is None


def test_service_chunk_via_hybrid_search_tool_path() -> None:
    """端到端：检索命中 → citation.chunk_id → chunk() 回溯原文。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("doc-1:3:0:16", "支持向量机是一种监督学习模型。")])
    service = KnowledgeService(index)

    hits = service.search("支持向量机", top_k=5)
    assert len(hits) == 1
    chunk_id = hits[0].citation.chunk_id

    original = service.chunk(chunk_id)
    assert original is not None
    assert original.content == "支持向量机是一种监督学习模型。"


def test_in_memory_chunks_of_document_orders_by_start() -> None:
    """InMemory 浏览：按 (start, chunk_id) 数字排序、只含目标文档（与 Sqlite 同约定）。

    用 start=100/20/0 验证是数字序而非 chunk_id 字典序（字典序下
    "…100…" 排在 "…20…" 前，数字序反之）。
    """
    index = InMemoryKnowledgeIndex()
    base = _chunk("c", "x", document_id="doc-1")
    index.upsert(
        [
            base.model_copy(update={"chunk_id": "doc-1:0:100:110", "start": 100}),
            base.model_copy(update={"chunk_id": "doc-1:0:20:30", "start": 20}),
            base.model_copy(update={"chunk_id": "doc-1:0:0:10", "start": 0}),
            base.model_copy(
                update={"chunk_id": "doc-2:0:0:10", "document_id": "doc-2"}
            ),
        ]
    )

    chunks = index.chunks_of_document("doc-1")

    assert [c.chunk_id for c in chunks] == [
        "doc-1:0:0:10",
        "doc-1:0:20:30",
        "doc-1:0:100:110",
    ]
    assert index.chunks_of_document("absent") == []


def test_hybrid_chunks_of_document_forwards_to_lexical(tmp_path: Path) -> None:
    """Hybrid 浏览：转发词法路——生产装配路径（Hybrid 包 Sqlite 词法）。"""
    db_path = tmp_path / "knowledge.db"
    lexical = SqliteKnowledgeIndex(db_path)
    lexical.upsert([_chunk("doc-1:0:0:10", "片段a", document_id="doc-1")])
    lexical.close()
    hybrid = HybridKnowledgeIndex(SqliteKnowledgeIndex(db_path), None)

    chunks = hybrid.chunks_of_document("doc-1")

    assert [c.chunk_id for c in chunks] == ["doc-1:0:0:10"]
    assert hybrid.chunks_of_document("absent") == []
    hybrid.close()
