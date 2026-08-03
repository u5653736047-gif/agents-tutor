"""SQLite 持久化词法索引测试（S3-T1 批量入库的存储底座）。

覆盖：检索语义与 InMemoryKnowledgeIndex 一致、同 chunk_id 覆盖、
整文档删除、关闭重开（进程重启）后数据仍在、完成标记的写入/查询/清除、
metadata JSON 往返。
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk


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


@pytest.fixture
def index(tmp_path: Path) -> Iterator[SqliteKnowledgeIndex]:
    """每个测试独立的临时 SQLite 索引。"""
    instance = SqliteKnowledgeIndex(tmp_path / "knowledge.db")
    yield instance
    instance.close()


def test_search_ranking_matches_in_memory_semantics(index: SqliteKnowledgeIndex) -> None:
    """同一批分块，SQLite 版与内存版的打分/排序/平局规则完全一致。"""
    chunks = [
        _chunk("chunk-c", "Force creates acceleration."),
        _chunk("chunk-b", "Mass and force determine acceleration."),
        _chunk("chunk-a", "Mass and force determine acceleration."),
    ]
    index.upsert(chunks)

    sqlite_hits = index.search("FORCE mass", top_k=3)
    memory_index = InMemoryKnowledgeIndex()
    memory_index.upsert(chunks)
    memory_hits = memory_index.search("FORCE mass", top_k=3)

    assert [hit.chunk.chunk_id for hit in sqlite_hits] == [
        hit.chunk.chunk_id for hit in memory_hits
    ]
    assert [hit.score for hit in sqlite_hits] == [hit.score for hit in memory_hits]
    assert [hit.citation.chunk_id for hit in sqlite_hits] == [
        hit.citation.chunk_id for hit in memory_hits
    ]


def test_upsert_replaces_an_existing_chunk_id(index: SqliteKnowledgeIndex) -> None:
    index.upsert([_chunk("shared", "old vocabulary", document_id="old-doc")])
    index.upsert([_chunk("shared", "new concept", document_id="new-doc")])

    assert index.search("old", top_k=5) == []
    hits = index.search("new", top_k=5)
    assert [hit.chunk.chunk_id for hit in hits] == ["shared"]
    assert hits[0].chunk.document_id == "new-doc"


def test_delete_document_removes_only_its_chunks(index: SqliteKnowledgeIndex) -> None:
    index.upsert(
        [
            _chunk("first", "shared term", document_id="doc-1"),
            _chunk("second", "shared term", document_id="doc-2"),
        ]
    )

    index.delete_document("doc-1")

    assert [hit.chunk.chunk_id for hit in index.search("shared", top_k=5)] == ["second"]


def test_chinese_bigram_search_and_top_k(index: SqliteKnowledgeIndex) -> None:
    index.upsert(
        [
            _chunk("mostly", "牛顿运动定律描述物体受力后的运动变化"),
            _chunk("partly", "牛顿研究了经典力学"),
        ]
    )

    hits = index.search("牛顿运动定律", top_k=1)

    assert [hit.chunk.chunk_id for hit in hits] == ["mostly"]
    assert hits[0].score > 0


def test_search_returns_empty_for_empty_index_or_query(
    index: SqliteKnowledgeIndex,
) -> None:
    assert index.search("anything", top_k=5) == []

    index.upsert([_chunk("chunk", "anything")])
    assert index.search("", top_k=5) == []
    assert index.search("   ", top_k=5) == []
    assert index.search("anything", top_k=0) == []


def test_data_persists_after_reopen(tmp_path: Path) -> None:
    """关闭连接后重开同一数据库文件：分块与完成标记都还在（进程重启可续用）。"""
    db_path = tmp_path / "kb.db"
    first = SqliteKnowledgeIndex(db_path)
    first.upsert([_chunk("c1", "牛顿运动定律", document_id="physics")])
    first.mark_document_complete("physics", chunk_count=1, page_count=1)
    first.close()

    second = SqliteKnowledgeIndex(db_path)
    try:
        assert [hit.chunk.chunk_id for hit in second.search("牛顿", top_k=5)] == ["c1"]
        assert second.is_document_complete("physics")
    finally:
        second.close()


def test_completion_markers_are_set_cleared_and_idempotent(
    index: SqliteKnowledgeIndex,
) -> None:
    assert not index.is_document_complete("doc-1")

    index.mark_document_complete("doc-1", chunk_count=3, page_count=2)
    # 重复写标记是幂等的（INSERT OR REPLACE 覆盖）。
    index.mark_document_complete("doc-1", chunk_count=4, page_count=3)
    assert index.is_document_complete("doc-1")

    index.clear_document_complete("doc-1")
    assert not index.is_document_complete("doc-1")
    # 清除不存在的标记也不报错。
    index.clear_document_complete("doc-1")


def test_markers_are_not_touched_by_chunk_operations(
    index: SqliteKnowledgeIndex,
) -> None:
    """分块层的 upsert/delete 不应影响完成标记（标记专属于入库流程）。"""
    index.upsert([_chunk("c1", "content", document_id="doc-1")])
    index.mark_document_complete("doc-1", chunk_count=1, page_count=1)

    index.upsert([_chunk("c2", "more", document_id="doc-1")])
    index.delete_document("doc-1")

    assert index.is_document_complete("doc-1")


def test_metadata_json_roundtrip(index: SqliteKnowledgeIndex) -> None:
    metadata = {
        "subject": "机器学习",
        "difficulty": "intermediate",
        "nested": {"key": "值"},
    }
    index.upsert([_chunk("c1", "支持向量机 间隔", metadata=metadata)])

    hit = index.search("支持向量机", top_k=5)[0]

    assert hit.chunk.metadata == metadata


def test_sqlite_index_thread_safe_concurrent_access(
    index: SqliteKnowledgeIndex,
) -> None:
    """多线程并发访问不崩溃、结果正确（T2 线程安全回归）。

    背景（面向初学者）：索引在 FastAPI lifespan（主线程）创建，而
    工具调用跑在 FastAPI 工作线程池——修复前（没有 check_same_thread
    =False）工作线程一调用就抛 ProgrammingError；即使允许跨线程，
    没有 RLock 串行化时并发操作同一连接也会数据错乱/崩溃。这里用
    线程池模拟真实并发：主线程建索引（fixture），8 个线程并发
    upsert/search/delete/chunks_of_document/完成标记操作，任何崩溃
    都会让本测试失败（修复前必失败）。
    """
    documents = 8
    rounds = 15

    def worker(doc_id: int) -> None:
        prefix = f"doc-{doc_id}"
        for round_no in range(rounds):
            # 每轮先整文档删除再写入：并发 delete + upsert 竞争同一连接。
            index.delete_document(prefix)
            chunk_id = f"{prefix}-{round_no}"
            # 查询词用线程内唯一 token（marker{doc_id}-{round_no}）：
            # 刚写入的 chunk 分数最高，top_k=3 必能命中，断言确定。
            index.upsert(
                [
                    _chunk(
                        chunk_id,
                        f"marker{doc_id}-{round_no} force mass",
                        document_id=prefix,
                    )
                ]
            )
            hits = index.search(f"marker{doc_id}-{round_no}", top_k=3)
            assert any(hit.chunk.chunk_id == chunk_id for hit in hits)
            # chunks_of_document 只含本线程的 doc（各线程 doc 互不干扰）。
            assert [c.chunk_id for c in index.chunks_of_document(prefix)] == [chunk_id]
        # 完成标记的写入/查询也走同一把锁（并发下互不干扰）。
        index.mark_document_complete(prefix, chunk_count=rounds, page_count=1)
        assert index.is_document_complete(prefix)

    with ThreadPoolExecutor(max_workers=documents) as pool:
        list(pool.map(worker, range(documents)))

    # 主线程复核：每个 doc 只剩最后一轮的分块，完成标记都在。
    for doc_id in range(documents):
        prefix = f"doc-{doc_id}"
        assert index.is_document_complete(prefix)
        assert [
            c.chunk_id for c in index.chunks_of_document(prefix)
        ] == [f"{prefix}-{rounds - 1}"]
    # 清除标记同样走锁，主线程再验证一次。
    index.clear_document_complete("doc-0")
    assert not index.is_document_complete("doc-0")
