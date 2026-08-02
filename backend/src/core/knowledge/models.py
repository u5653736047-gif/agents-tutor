"""Knowledge-base data contracts shared by loaders, indexes, and tools."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, Field, field_validator


def _validate_logical_source(source: str) -> str:
    """Reject local filesystem locations at every public model boundary."""
    candidate = source.strip()
    if not candidate or candidate != source or not candidate.isprintable():
        raise ValueError("source must be a logical identifier, not a filesystem path")

    windows_path = PureWindowsPath(candidate)
    posix_path = PurePosixPath(candidate)
    if (
        windows_path.drive
        or windows_path.root
        or posix_path.is_absolute()
        or candidate.casefold().startswith("file:")
    ):
        raise ValueError("source must be a logical identifier, not a filesystem path")
    return candidate


class _LogicalSourceModel(BaseModel):
    """Enforce logical source identifiers at every knowledge-model boundary."""

    source: str

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return _validate_logical_source(value)


class KnowledgeDocument(_LogicalSourceModel):
    """A source document, or one page of a paged source."""

    document_id: str
    content: str
    page: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeChunk(_LogicalSourceModel):
    """A searchable slice with coordinates in its source document."""

    chunk_id: str
    document_id: str
    content: str
    page: int | None = Field(default=None, ge=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(_LogicalSourceModel):
    """Minimal source information safe to expose with a search result."""

    document_id: str
    page: int | None = Field(default=None, ge=1)
    chunk_id: str


class SearchHit(BaseModel):
    """A ranked chunk paired with its citation."""

    chunk: KnowledgeChunk
    citation: Citation
    score: float = Field(ge=0)
