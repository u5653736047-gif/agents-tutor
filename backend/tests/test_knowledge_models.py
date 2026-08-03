"""Knowledge-layer data contract tests."""

import pytest
from pydantic import ValidationError

from core.knowledge.models import Citation, KnowledgeChunk, KnowledgeDocument, SearchHit


def test_document_has_safe_defaults() -> None:
    first = KnowledgeDocument(document_id="guide", content="Newton's laws", source="guide.txt")
    second = KnowledgeDocument(document_id="notes", content="Force", source="notes.txt")

    first.metadata["subject"] = "physics"

    assert first.page is None
    assert first.metadata == {"subject": "physics"}
    assert second.metadata == {}


def test_chunk_and_citation_keep_source_coordinates() -> None:
    chunk = KnowledgeChunk(
        chunk_id="guide:2:0:12",
        document_id="guide",
        content="Newton's law",
        source="guide.pdf",
        page=2,
        start=0,
        end=12,
        metadata={"subject": "physics"},
    )
    citation = Citation(
        document_id=chunk.document_id,
        source=chunk.source,
        page=chunk.page,
        chunk_id=chunk.chunk_id,
    )

    assert citation.model_dump() == {
        "document_id": "guide",
        "source": "guide.pdf",
        "page": 2,
        "chunk_id": "guide:2:0:12",
    }


def test_search_hit_rejects_negative_score() -> None:
    chunk = KnowledgeChunk(
        chunk_id="guide:0:0:5",
        document_id="guide",
        content="Force",
        source="guide.txt",
        page=None,
        start=0,
        end=5,
    )
    citation = Citation(
        document_id="guide",
        source="guide.txt",
        page=None,
        chunk_id=chunk.chunk_id,
    )

    with pytest.raises(ValidationError):
        SearchHit(chunk=chunk, citation=citation, score=-0.1)


@pytest.mark.parametrize(
    "unsafe_source",
    [
        r"C:\private\knowledge\guide.pdf",
        r"C:private\knowledge\guide.pdf",
        r"\\server\private-share\guide.pdf",
        r"\private\knowledge\guide.pdf",
        "/srv/private/knowledge/guide.pdf",
        "file:///srv/private/knowledge/guide.pdf",
        r" C:\private\knowledge\guide.pdf",
        " /srv/private/knowledge/guide.pdf",
        " FILE:///srv/private/knowledge/guide.pdf",
        "\nC:\\private\\knowledge\\guide.pdf",
        "\t/srv/private/knowledge/guide.pdf",
        "course://lesson\nprivate",
        "",
        "   ",
    ],
)
def test_knowledge_models_reject_unsafe_sources(
    unsafe_source: str,
) -> None:
    with pytest.raises(ValidationError, match="logical identifier"):
        KnowledgeDocument(
            document_id="guide",
            content="Force",
            source=unsafe_source,
        )
    with pytest.raises(ValidationError, match="logical identifier"):
        KnowledgeChunk(
            chunk_id="guide:0:0:5",
            document_id="guide",
            content="Force",
            source=unsafe_source,
            start=0,
            end=5,
        )
    with pytest.raises(ValidationError, match="logical identifier"):
        Citation(
            document_id="guide",
            source=unsafe_source,
            chunk_id="guide:0:0:5",
        )
