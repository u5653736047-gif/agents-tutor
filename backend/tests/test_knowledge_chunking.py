"""Deterministic knowledge chunking tests."""

import pytest

from core.knowledge.chunking import chunk_document, chunk_documents
from core.knowledge.models import KnowledgeDocument


@pytest.mark.parametrize(
    ("chunk_size", "overlap"),
    [(0, 0), (-1, 0), (4, -1), (4, 4), (4, 5)],
)
def test_chunk_document_rejects_invalid_window(chunk_size: int, overlap: int) -> None:
    document = KnowledgeDocument(document_id="doc", content="content", source="doc.txt")

    with pytest.raises(ValueError):
        chunk_document(document, chunk_size=chunk_size, overlap=overlap)


def test_chunk_document_is_deterministic_and_keeps_metadata() -> None:
    document = KnowledgeDocument(
        document_id="lesson",
        content="abcdefghij",
        source="lesson.pdf",
        page=2,
        metadata={"subject": "physics"},
    )

    first = chunk_document(document, chunk_size=4, overlap=1)
    second = chunk_document(document, chunk_size=4, overlap=1)

    assert first == second
    assert [(item.content, item.start, item.end, item.chunk_id) for item in first] == [
        ("abcd", 0, 4, "lesson:2:0:4"),
        ("defg", 3, 7, "lesson:2:3:7"),
        ("ghij", 6, 10, "lesson:2:6:10"),
    ]
    assert all(item.source == "lesson.pdf" and item.page == 2 for item in first)
    assert all(item.metadata == {"subject": "physics"} for item in first)


def test_chunk_documents_preserves_document_order() -> None:
    documents = [
        KnowledgeDocument(document_id="first", content="abcd", source="first.txt"),
        KnowledgeDocument(document_id="second", content="efgh", source="second.txt"),
    ]

    chunks = chunk_documents(documents, chunk_size=3, overlap=0)

    assert [item.chunk_id for item in chunks] == [
        "first:0:0:3",
        "first:0:3:4",
        "second:0:0:3",
        "second:0:3:4",
    ]
