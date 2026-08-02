"""Replaceable index contract and a dependency-free in-memory implementation."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

from .models import Citation, KnowledgeChunk, SearchHit

_ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+")
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")


class KnowledgeIndex(Protocol):
    """Small contract that future vector indexes can implement."""

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        """Insert chunks, replacing an existing chunk with the same ID."""
        ...

    def delete_document(self, document_id: str) -> None:
        """Delete every chunk belonging to a document."""
        ...

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        """Return the highest-scoring chunks for a query."""
        ...


class InMemoryKnowledgeIndex:
    """Simple lexical index for local development and deterministic tests."""

    def __init__(self) -> None:
        self._chunks: dict[str, KnowledgeChunk] = {}

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk

    def delete_document(self, document_id: str) -> None:
        self._chunks = {
            chunk_id: chunk
            for chunk_id, chunk in self._chunks.items()
            if chunk.document_id != document_id
        }

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        if not query.strip() or top_k <= 0:
            return []

        query_terms = _lexical_terms(query)
        if not query_terms:
            return []

        hits: list[SearchHit] = []
        for chunk in self._chunks.values():
            # Shared terms are enough for a small, predictable lexical baseline.
            score = float(len(query_terms & _lexical_terms(chunk.content)))
            if score <= 0:
                continue
            hits.append(
                SearchHit(
                    chunk=chunk,
                    citation=Citation(
                        document_id=chunk.document_id,
                        source=chunk.source,
                        page=chunk.page,
                        chunk_id=chunk.chunk_id,
                    ),
                    score=score,
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        return hits[:top_k]


def _lexical_terms(text: str) -> set[str]:
    """Extract lowercase English words plus Chinese characters and pairs."""
    terms = {match.group().lower() for match in _ENGLISH_WORD.finditer(text)}
    for match in _CHINESE_RUN.finditer(text):
        run = match.group()
        terms.update(run)
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


__all__ = ["InMemoryKnowledgeIndex", "KnowledgeIndex"]
