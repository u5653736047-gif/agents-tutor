"""I1 知识库清单/统计能力测试：SqliteKnowledgeCatalog + merge_uploaded_catalog。

覆盖：
- list_documents：脚本入库(有 ingest_marks + metadata 注入)的文档
  聚合出 title/subjects/difficulty/chunk_count/page_count/ingested_at；
  API 上传文档(无 ingest_marks、无 metadata)聚合出空元数据字段；
- document_stats：total_documents / total_chunks / total_pages /
  frontmatter_chunks 的聚合口径（frontmatter 单独计数、页数只统计
  page 非空、无页概念的 txt 不计 total_pages）；
- 空库（从未入库的表结构不存在的文件）：清单空、统计全 0，不报错；
- merge_uploaded_catalog：索引库聚合优先、上传条目兜底、按
  document_id 去重与排序；
- source 脱敏：含文件系统路径的 source 在构造 KnowledgeDocumentInfo
  时被拒绝（models 层校验，防清单泄漏路径）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.knowledge.catalog import (
    KnowledgeDocumentInfo,
    SqliteKnowledgeCatalog,
    merge_uploaded_catalog,
)
from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
    page: int | None = None,
    metadata: dict[str, object] | None = None,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=document_id,
        page=page,
        start=0,
        end=len(content),
        metadata=metadata or {},
    )


def _make_library(path: Path) -> None:
    """构造与 ingest 产物同构的词法库：两本书（一本有 frontmatter）+ 一本上传文档。"""
    index = SqliteKnowledgeIndex(path)
    index.upsert(
        [
            _chunk(
                "ml-1",
                "支持向量机是一种监督学习模型。",
                document_id="ml-zhouzhihua",
                page=1,
                metadata={
                    "subject": "机器学习,统计学习",
                    "difficulty": "intermediate",
                    "title": "机器学习",
                },
            ),
            _chunk(
                "ml-2",
                "间隔最大化是支持向量机的核心。",
                document_id="ml-zhouzhihua",
                page=2,
                metadata={
                    "subject": "机器学习,统计学习",
                    "difficulty": "intermediate",
                    "title": "机器学习",
                },
            ),
            # frontmatter 噪音 chunk：chunk_class 标记后单独计数。
            _chunk(
                "ml-0",
                "目录 1 ...... 1",
                document_id="ml-zhouzhihua",
                page=1,
                metadata={
                    "subject": "机器学习,统计学习",
                    "difficulty": "intermediate",
                    "title": "机器学习",
                    "chunk_class": "frontmatter",
                },
            ),
            # API 上传文档：无 ingest_marks、无注入 metadata。
            _chunk("guide-1", "上传的讲义内容。", document_id="guide", page=None),
        ]
    )
    index.mark_document_complete(
        "ml-zhouzhihua", chunk_count=3, page_count=2
    )
    index.close()


def test_list_documents_aggregates_script_and_uploaded_docs(tmp_path: Path) -> None:
    db_path = tmp_path / "knowledge.db"
    _make_library(db_path)
    catalog = SqliteKnowledgeCatalog(db_path)

    documents = catalog.list_documents()

    # 按 document_id 排序：guide 在前（字典序）。
    assert [doc.document_id for doc in documents] == ["guide", "ml-zhouzhihua"]
    # 脚本入库的书：chunk_count 聚合正确、元数据注入、ingest 时间有值。
    book = documents[1]
    assert book.source == "ml-zhouzhihua"
    assert book.chunk_count == 3
    assert book.page_count == 2
    assert book.title == "机器学习"
    # subject 字段以逗号连接多学科（ingest 注入约定），catalog 拆分为列表。
    assert book.subjects == ["机器学习", "统计学习"]
    assert book.difficulty == "intermediate"
    assert book.ingested_at is not None
    # 上传文档：chunk_count 聚合、无元数据字段。
    uploaded = documents[0]
    assert uploaded.chunk_count == 1
    assert uploaded.page_count is None
    assert uploaded.title is None
    assert uploaded.subjects == []
    assert uploaded.difficulty is None
    assert uploaded.ingested_at is None
    catalog.close()


def test_document_stats_aggregation(tmp_path: Path) -> None:
    db_path = tmp_path / "knowledge.db"
    _make_library(db_path)
    catalog = SqliteKnowledgeCatalog(db_path)

    stats = catalog.document_stats()

    assert stats.total_documents == 2
    assert stats.total_chunks == 4
    # 页数只统计 page 非空的 chunk 去重页号：ml 的 {1,2} 两页，guide 无页。
    assert stats.total_pages == 2
    # frontmatter 单独计数（不混入 total_chunks 口径）。
    assert stats.frontmatter_chunks == 1
    catalog.close()


def test_empty_database_returns_zeroes(tmp_path: Path) -> None:
    """从未入库的空库文件（表不存在）：清单空、统计全 0，不报错。"""
    db_path = tmp_path / "empty.db"
    # 空文件（SQLite 打开会自动初始化，无业务表）。
    db_path.write_bytes(b"")
    catalog = SqliteKnowledgeCatalog(db_path)

    assert catalog.list_documents() == []
    stats = catalog.document_stats()
    assert stats.total_documents == 0
    assert stats.total_chunks == 0
    assert stats.total_pages is None
    assert stats.frontmatter_chunks == 0
    catalog.close()


def test_merge_uploaded_catalog_prefers_index_and_dedups(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "knowledge.db"
    _make_library(db_path)
    catalog = SqliteKnowledgeCatalog(db_path)
    catalog_docs = catalog.list_documents()

    # 上传回执级条目：guide 已入库（应被索引库聚合覆盖）、
    # draft 未入库（应兜底保留）。
    uploaded = [
        KnowledgeDocumentInfo(
            document_id="guide",
            source="guide",
            chunk_count=99,  # 错误值：应被索引库聚合值覆盖
        ),
        KnowledgeDocumentInfo(
            document_id="draft",
            source="draft",
            chunk_count=2,
        ),
    ]

    merged = merge_uploaded_catalog(catalog_docs, uploaded)

    assert [doc.document_id for doc in merged] == ["draft", "guide", "ml-zhouzhihua"]
    guide = next(doc for doc in merged if doc.document_id == "guide")
    assert guide.chunk_count == 1  # 索引库聚合优先，不用上传回执的 99
    draft = next(doc for doc in merged if doc.document_id == "draft")
    assert draft.chunk_count == 2
    catalog.close()


def test_document_info_rejects_filesystem_path_source() -> None:
    """source 含文件系统路径时构造被拒绝（models 层脱敏校验，防清单泄漏）。"""
    with pytest.raises(ValueError):
        KnowledgeDocumentInfo(
            document_id="bad",
            source=r"C:\Users\runner\secret.txt",
        )
