"""Tests for the replaceable knowledge index contract."""

from __future__ import annotations

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=f"{document_id}.txt",
        page=None,
        start=0,
        end=len(content),
    )


def test_search_ranks_english_overlap_and_uses_chunk_id_for_ties() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("chunk-c", "Force creates acceleration."),
            _chunk("chunk-b", "Mass and force determine acceleration."),
            _chunk("chunk-a", "Mass and force determine acceleration."),
        ]
    )

    hits = index.search("FORCE mass", top_k=3)

    assert [hit.chunk.chunk_id for hit in hits] == ["chunk-a", "chunk-b", "chunk-c"]
    assert hits[0].score == hits[1].score > hits[2].score > 0
    assert hits[0].citation.chunk_id == "chunk-a"
    assert hits[0].citation.document_id == "doc-1"


def test_search_uses_chinese_bigrams_and_honors_top_k() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("mostly-related", "牛顿运动定律描述物体受力后的运动变化"),
            _chunk("partly-related", "牛顿研究了经典力学"),
            _chunk("unrelated", "化学反应需要满足守恒关系"),
        ]
    )

    hits = index.search("牛顿运动定律", top_k=1)

    assert [hit.chunk.chunk_id for hit in hits] == ["mostly-related"]
    assert hits[0].score > 0


def test_search_keeps_single_chinese_characters_searchable() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("mechanics", "力可以改变物体的运动状态")])

    hits = index.search("力", top_k=5)

    assert [hit.chunk.chunk_id for hit in hits] == ["mechanics"]


def test_upsert_replaces_an_existing_chunk_id() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("shared", "old vocabulary", document_id="old-doc")])
    replacement = _chunk("shared", "new concept", document_id="new-doc")

    index.upsert([replacement])

    assert index.search("old", top_k=5) == []
    hits = index.search("new", top_k=5)
    assert [hit.chunk for hit in hits] == [replacement]


def test_delete_document_removes_only_its_chunks() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("first", "shared term", document_id="doc-1"),
            _chunk("second", "shared term", document_id="doc-2"),
        ]
    )

    index.delete_document("doc-1")

    assert [hit.chunk.chunk_id for hit in index.search("shared", top_k=5)] == ["second"]


def test_search_returns_empty_for_empty_index_or_query() -> None:
    index = InMemoryKnowledgeIndex()

    assert index.search("anything", top_k=5) == []

    index.upsert([_chunk("chunk", "anything")])
    assert index.search("", top_k=5) == []
    assert index.search("   ", top_k=5) == []
