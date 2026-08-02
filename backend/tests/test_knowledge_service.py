"""Tests for the knowledge ingestion and retrieval service."""

from __future__ import annotations

import pytest

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeDocument
from core.knowledge.service import KnowledgeService


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
    assert hits[0].chunk.document_id == "physics"
    assert hits[0].citation.source == "inline:physics"

    service.delete_document("physics")
    assert service.search("force motion", top_k=5) == []


def test_reimport_replaces_stale_chunks_for_the_same_document() -> None:
    service = KnowledgeService(
        InMemoryKnowledgeIndex(),
        chunk_size=10,
        overlap=0,
    )
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="lesson",
                content="current---stale-tail",
                source="lesson.txt",
            )
        ]
    )

    service.add_documents(
        [
            KnowledgeDocument(
                document_id="lesson",
                content="fresh",
                source="lesson.txt",
            )
        ]
    )

    assert service.search("stale", top_k=5) == []
    assert service.search("fresh", top_k=5)


def test_one_batch_keeps_all_pages_with_the_same_document_id() -> None:
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=20, overlap=0)

    service.add_documents(
        [
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
    )

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
