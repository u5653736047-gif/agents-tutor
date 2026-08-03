"""Replaceable index contract and a dependency-free in-memory implementation."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
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


class SqliteKnowledgeIndex:
    """SQLite 持久化词法索引：实现 KnowledgeIndex 协议，检索语义与 InMemory 一致。

    用途：批量入库脚本把教材分块持久化到磁盘，进程退出后数据仍在，
    下次打开同一数据库文件即可继续检索（无需重新解析 PDF）。

    除 chunk 表外维护 ingest_marks 完成标记表：脚本只有把整本书全部
    入库成功后才写标记，检索/删除分块的操作都不触碰该表，因此
    「已完成标记」专属于入库流程（详见 scripts/ingest_books.py 注释）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        # WAL 模式下读操作不阻塞写操作，对脚本与后续检索并发更友好。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        # chunk 表：一条记录一个分块，chunk_id 为主键（整文档替换时覆盖）。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL,
                page INTEGER,
                start INTEGER NOT NULL,
                end INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        # ingest_marks 表：整本书入库成功的完成标记（续跑跳过依据）。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_marks (
                document_id TEXT PRIMARY KEY,
                chunk_count INTEGER NOT NULL,
                page_count INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        """插入分块：同 chunk_id 直接覆盖（INSERT OR REPLACE），单事务原子提交。"""
        rows = [
            (
                chunk.chunk_id,
                chunk.document_id,
                chunk.content,
                chunk.source,
                chunk.page,
                chunk.start,
                chunk.end,
                json.dumps(chunk.metadata, ensure_ascii=False),
            )
            for chunk in chunks
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO chunks "
            "(chunk_id, document_id, content, source, page, start, end, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def delete_document(self, document_id: str) -> None:
        """删除某个 document_id 的全部 chunk（整文档替换语义的删除半段）。"""
        self._conn.execute(
            "DELETE FROM chunks WHERE document_id = ?", (document_id,)
        )
        self._conn.commit()

    def search(self, query: str, top_k: int) -> list[SearchHit]:
        """词法检索：打分与排序规则与 InMemoryKnowledgeIndex 完全一致。"""
        if not query.strip() or top_k <= 0:
            return []

        query_terms = _lexical_terms(query)
        if not query_terms:
            return []

        scored: list[tuple[float, str, KnowledgeChunk]] = []
        for row in self._conn.execute(
            "SELECT chunk_id, document_id, content, source, page, start, end, "
            "metadata_json FROM chunks"
        ):
            (
                chunk_id,
                document_id,
                content,
                source,
                page,
                start,
                end,
                metadata_json,
            ) = row
            # 与内存版相同：命中词数即分数，不命中的分块直接跳过。
            score = float(len(query_terms & _lexical_terms(content)))
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    chunk_id,
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        content=content,
                        source=source,
                        page=page,
                        start=start,
                        end=end,
                        metadata=json.loads(metadata_json),
                    ),
                )
            )

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
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
            for score, _, chunk in scored[:top_k]
        ]

    # ── 入库完成标记（供批量入库脚本实现「已入库跳过 / 失败续跑」）──

    def is_document_complete(self, document_id: str) -> bool:
        """该 document_id 是否已有「整本入库成功」的完成标记。"""
        row = self._conn.execute(
            "SELECT 1 FROM ingest_marks WHERE document_id = ?", (document_id,)
        ).fetchone()
        return row is not None

    def mark_document_complete(
        self,
        document_id: str,
        *,
        chunk_count: int,
        page_count: int,
    ) -> None:
        """写入完成标记（幂等：重复调用直接覆盖旧标记）。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO ingest_marks "
            "(document_id, chunk_count, page_count, completed_at) VALUES (?, ?, ?, ?)",
            (
                document_id,
                chunk_count,
                page_count,
                datetime.now(UTC).isoformat(),
            ),
        )
        self._conn.commit()

    def clear_document_complete(self, document_id: str) -> None:
        """清除完成标记：--force 重入库前调用，保证中途失败不会误跳过。"""
        self._conn.execute(
            "DELETE FROM ingest_marks WHERE document_id = ?", (document_id,)
        )
        self._conn.commit()

    def close(self) -> None:
        """关闭底层数据库连接。"""
        self._conn.close()


def _lexical_terms(text: str) -> set[str]:
    """Extract lowercase English words plus Chinese characters and pairs."""
    terms = {match.group().lower() for match in _ENGLISH_WORD.finditer(text)}
    for match in _CHINESE_RUN.finditer(text):
        run = match.group()
        terms.update(run)
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


__all__ = ["InMemoryKnowledgeIndex", "KnowledgeIndex", "SqliteKnowledgeIndex"]
