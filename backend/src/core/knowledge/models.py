"""Knowledge-base data contracts shared by loaders, indexes, and tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class KnowledgeDocument(BaseModel):
    """A source document, or one page of a paged source."""

    document_id: str
    content: str
    source: str
    page: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunk(BaseModel):
    """A searchable slice with coordinates in its source document."""

    chunk_id: str
    document_id: str
    content: str
    source: str
    page: int | None = Field(default=None, ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    """Minimal source information safe to expose with a search result."""

    document_id: str
    source: str
    page: int | None = Field(default=None, ge=1)
    chunk_id: str


class SearchHit(BaseModel):
    """A ranked chunk paired with its citation."""

    chunk: KnowledgeChunk
    citation: Citation
    score: float = Field(ge=0)
