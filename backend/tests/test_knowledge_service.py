"""Tests for the knowledge ingestion and retrieval service."""

from __future__ import annotations

import pytest

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument
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
