"""知识库文档清单与统计能力（I1）：把脚本入库与 API 上传的文档统一暴露。

背景（面向初学者）：
- core 的 KnowledgeIndex 协议只提供 upsert / delete_document / search，
  REST 层 GET /knowledge/documents 过去只能靠 API 层进程内注册表
  （只登记上传文档，脚本入库的教材对教师 UI 不可见，且重启即失）。
- 本模块补上「文档枚举 / 语料统计」：直接从词法库 SQLite 聚合
  （chunks 表按 source 分组 + ingest_marks 表 + metadata 的 title），
  与索引共用同一库文件，零 schema 变更、零额外持久化。
- 线程安全：与 SqliteKnowledgeIndex 完全同一约定——check_same_thread
  =False 允许跨线程，RLock 串行化所有 self._conn 访问（索引在
  FastAPI lifespan 主线程创建、REST 查询跑工作线程池，见 index.py
  的线程安全说明）。

与 API 层注册表的关系：catalog 只读词法库，API 上传文档若已写入
索引则自然出现在聚合结果里（无需合并）；仅当上传文档写入失败或
尚未落库时才需要调用方（api/knowledge.py）用 merge_uploaded_catalog
把注册表条目并进来。合并是纯内存拼接，不写库。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .models import _validate_logical_source


class NamespaceInfo(BaseModel):
    """一个知识空间的聚合信息（只读）。

    - namespace：空间标识（"public" 为保留值，见计划 C1 决策 1）；
    - document_count：该空间内的文档数（按 document_id 去重）。
    """

    namespace: str
    document_count: int


class KnowledgeDocumentInfo(BaseModel):
    """一篇知识文档的元数据（只读，不含内容与文件路径）。

    字段说明（面向初学者）：
    - document_id / source：逻辑标识（同一值，见 models.py 约定）；
    - title：书名（metadata 注入，脚本入库有，API 上传可能没有）；
    - page_count / chunk_count：词法库聚合的页数与分块数；
    - subjects：学科标签（metadata 注入）；
    - difficulty：难度（metadata 注入）；
    - ingested_at：入库完成时间（来自 ingest_marks 表；无标记的
      API 上传文档为 None）。
    """

    document_id: str
    source: str
    title: str | None = None
    page_count: int | None = None
    chunk_count: int = 0
    subjects: list[str] = Field(default_factory=list)
    difficulty: str | None = None
    ingested_at: str | None = None

    def __init__(self, **data: object) -> None:
        # source 复用 models._validate_logical_source 的脱敏校验：
        # 任何来源构造的清单条目都不得携带文件系统路径。
        super().__init__(**data)
        self.source = _validate_logical_source(self.source)


class KnowledgeTreeSection(BaseModel):
    """知识树小节节点：section 编号 + 该节 chunk 数与概念标签汇总。"""

    section: str
    chunk_count: int = 0
    tags: list[str] = Field(default_factory=list)


class KnowledgeTreeChapter(BaseModel):
    """知识树章节点：chapter 标识 + 小节列表与自身 chunk 计数。

    chunk_count 含直接挂在章上（无 section 归属）的 chunk。
    """

    chapter: str
    chunk_count: int = 0
    sections: list[KnowledgeTreeSection] = Field(default_factory=list)


class KnowledgeDocumentTree(BaseModel):
    """文档结构树响应（S5-C2）：tree=有章节层级；flat=无结构按页平铺。

    判别字段 kind 决定 chapters 与 flat_pages 哪个字段有效。
    """

    kind: Literal["tree", "flat"]
    document_id: str
    chapters: list[KnowledgeTreeChapter] = Field(default_factory=list)
    flat_pages: list[int] = Field(default_factory=list)


class KnowledgeBaseStats(BaseModel):
    """知识库语料统计（教师端总览卡数据源）。

    total_chunks 与 total_pages 含 frontmatter 噪音分块（它们在库里、
    可被检索工具显式过滤，统计口径按「库中实际内容」如实呈现）；
    frontmatter_chunks 单独给出，供教师了解噪音占比。
    """

    total_documents: int = 0
    total_chunks: int = 0
    total_pages: int | None = None  # 含页概念的文档页数合计（txt 无页概念不计）
    frontmatter_chunks: int = 0


class KnowledgeCatalog(Protocol):
    """文档清单 / 语料统计协议：任何实现只需提供这两个只读方法。

    与 KnowledgeIndex 同一注入风格（鸭子类型）：未来换存储（如
    向量库）或换实现（如接 manifest 元数据）不需要改调用方。
    """

    def list_namespaces(self) -> list[NamespaceInfo]:
        """聚合全部知识空间及各空间的文档数（按 namespace 排序稳定）。"""
        ...

    def list_documents(self) -> list[KnowledgeDocumentInfo]:
        """返回全部文档的元数据清单（顺序稳定：按 document_id）。"""
        ...

    def document_tree(self, document_id: str) -> KnowledgeDocumentTree:
        """聚合单篇文档的章节层级树（无结构文档回退按页平铺）。"""
        ...

    def document_stats(self) -> KnowledgeBaseStats:
        """返回语料统计（文档数 / 分块数 / 页数 / frontmatter 分块数）。"""
        ...


class _TreeBucket:
    """树聚合中间态：一个挂载点（章或节）的 chunk 计数与标签去重列表。"""

    def __init__(self) -> None:
        self.count = 0
        self.tags: list[str] = []

    def add(self, tags: object) -> None:
        self.count += 1
        if not isinstance(tags, list):
            return
        for tag in tags:
            if isinstance(tag, str) and tag not in self.tags:
                self.tags.append(tag)


class SqliteKnowledgeCatalog:
    """从词法库 SQLite 聚合文档清单与统计（实现 KnowledgeCatalog 协议）。

    数据来源（全部只读词法库，与 SqliteKnowledgeIndex 同一文件）：
    - chunks 表：按 source 分组统计 chunk_count / page_count（页数
      只统计 page 非空的 chunk 去重页号）、frontmatter 分块数；
    - ingest_marks 表：completed_at 入库时间（有标记的文档）；
    - metadata_json：title / subject / difficulty（脚本入库注入的
      领域字段，subject 以逗号连接多学科，见 models.py 模块注释）。

    构造即建立连接并建表（与 SqliteKnowledgeIndex 同一行为，确保
    只读场景也有可用的连接）；close() 关闭连接。线程安全约定与
    SqliteKnowledgeIndex 完全一致（见模块 docstring 与 index.py）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
        # 只读查询不依赖表结构存在与否：表缺失（从未入库的空库）时
        # 聚合查询直接返回空，不报错——与「空库检索返回空」语义一致。
        self._tables_exist = self._check_tables()

    def _check_tables(self) -> bool:
        """chunks / ingest_marks 表是否已存在（从未入库的空库为 False）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('chunks', 'ingest_marks')"
            ).fetchone()
        return bool(row and row[0] == 2)

    def list_namespaces(self) -> list[NamespaceInfo]:
        """聚合全部知识空间及各空间的文档数（按 namespace 排序稳定）。

        数据源与 list_documents 相同（chunks 表 metadata_json 的
        namespace 键；C1 决策 3 的回填迁移保证存量行均有该键）。
        空库返回空列表。
        """
        if not self._tables_exist:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(json_extract(metadata_json, '$.namespace'), 'public') "
                "AS namespace, COUNT(DISTINCT document_id) "
                "FROM chunks GROUP BY namespace ORDER BY namespace"
            ).fetchall()
        return [
            NamespaceInfo(namespace=namespace, document_count=int(count))
            for namespace, count in rows
        ]

    def document_tree(self, document_id: str) -> KnowledgeDocumentTree:
        """聚合单篇文档的章→节两级树；无章节时回退按页平铺（S5-C2）。

        数据源为 chunks.metadata_json 的 chapter/section/tags 键
        （chunking.py 标题行规则提取）。判定规则：任一 chunk 带 chapter
        或 section 即按树形态聚合；否则回退 flat 页列表。文档不存在或
        无 chunk 时返回空树（kind="flat"、flat_pages 为空——调用方据此
        渲染「无内容」占位而非报错）。
        """
        if not self._tables_exist:
            return KnowledgeDocumentTree(
                kind="flat", document_id=document_id, flat_pages=[]
            )
        with self._lock:
            rows = self._conn.execute(
                "SELECT page, metadata_json FROM chunks "
                "WHERE document_id = ? ORDER BY chunk_id",
                (document_id,),
            ).fetchall()

        chapters: dict[str, dict[str, _TreeBucket]] = {}
        chapter_order: list[str] = []
        section_order: dict[str, list[str]] = {}
        pages_without_structure: list[int] = []
        has_structure = False
        for page, metadata_json in rows:
            meta = json.loads(metadata_json) if metadata_json else {}
            chapter = meta.get("chapter")
            section = meta.get("section")
            if (isinstance(chapter, str) and chapter.strip()) or (
                isinstance(section, str) and section.strip()
            ):
                has_structure = True
            else:
                # txt 上传件无页概念（page=None）：不进 flat 页列表，
                # 该文档回退为空 flat 形态（前端渲染无内容占位）。
                if page is not None:
                    pages_without_structure.append(int(page))
                continue
            ch_key = chapter.strip() if isinstance(chapter, str) else ""
            sec_key = section.strip() if isinstance(section, str) else ""
            if ch_key not in chapters:
                chapters[ch_key] = {}
                chapter_order.append(ch_key)
                section_order[ch_key] = []
            if sec_key and sec_key not in section_order[ch_key]:
                section_order[ch_key].append(sec_key)
            mount_key = sec_key if sec_key else "_direct"
            chapters[ch_key].setdefault(mount_key, _TreeBucket()).add(
                meta.get("tags")
            )

        # 无任何结构标记 → 按页平铺回退（去重升序）。
        if not has_structure:
            return KnowledgeDocumentTree(
                kind="flat",
                document_id=document_id,
                flat_pages=sorted(set(pages_without_structure)),
            )

        tree_chapters: list[KnowledgeTreeChapter] = []
        for ch_key in chapter_order:
            bucket = chapters[ch_key]
            direct = bucket.get("_direct", _TreeBucket())
            chapter_count = direct.count
            sections: list[KnowledgeTreeSection] = []
            for sec_key in section_order[ch_key]:
                entry = bucket[sec_key]
                chapter_count += entry.count
                sections.append(
                    KnowledgeTreeSection(
                        section=sec_key,
                        chunk_count=entry.count,
                        tags=entry.tags,
                    )
                )
            tree_chapters.append(
                KnowledgeTreeChapter(
                    chapter=ch_key if ch_key else "未分章",
                    chunk_count=chapter_count,
                    sections=sections,
                )
            )
        return KnowledgeDocumentTree(
            kind="tree",
            document_id=document_id,
            chapters=tree_chapters,
        )

    def list_documents(self) -> list[KnowledgeDocumentInfo]:
        """聚合全部文档元数据，按 document_id 排序（顺序稳定）。"""
        if not self._tables_exist:
            return []
        with self._lock:
            chunk_rows = self._conn.execute(
                "SELECT document_id, MIN(source), COUNT(*), COUNT(DISTINCT page), "
                "SUM(CASE WHEN json_extract(metadata_json, '$.chunk_class') = "
                "'frontmatter' THEN 1 ELSE 0 END), "
                "MIN(metadata_json) "
                "FROM chunks GROUP BY document_id ORDER BY document_id"
            ).fetchall()
            mark_rows = {
                document_id: completed_at
                for document_id, completed_at in self._conn.execute(
                    "SELECT document_id, completed_at FROM ingest_marks"
                ).fetchall()
            }
        documents: list[KnowledgeDocumentInfo] = []
        for (
            document_id,
            source,
            chunk_count,
            page_count,
            _frontmatter_count,
            metadata_json,
        ) in chunk_rows:
            metadata = json.loads(metadata_json) if metadata_json else {}
            title = metadata.get("title")
            subject = metadata.get("subject")
            difficulty = metadata.get("difficulty")
            documents.append(
                KnowledgeDocumentInfo(
                    document_id=document_id,
                    # source 展示值取 MIN(source)：脚本书目两者相等零变化；
                    # 非 public 上传（source=文件名、document_id 带前缀）
                    # 不再与同名其他空间条目错误合并。
                    source=source,
                    title=str(title) if title is not None else None,
                    page_count=int(page_count) if page_count else None,
                    chunk_count=int(chunk_count),
                    subjects=_split_subjects(subject),
                    difficulty=str(difficulty) if difficulty is not None else None,
                    ingested_at=mark_rows.get(document_id),
                )
            )
        return documents


    def document_stats(self) -> KnowledgeBaseStats:
        """语料统计：文档数 / 分块数 / 页数 / frontmatter 分块数。"""
        if not self._tables_exist:
            return KnowledgeBaseStats()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT document_id), "
                "COUNT(DISTINCT page), "
                "SUM(CASE WHEN json_extract(metadata_json, '$.chunk_class') = "
                "'frontmatter' THEN 1 ELSE 0 END) FROM chunks"
            ).fetchone()
        total_chunks = int(row[0])
        total_documents = int(row[1])
        distinct_pages = int(row[2])
        frontmatter_chunks = int(row[3] or 0)
        return KnowledgeBaseStats(
            total_documents=total_documents,
            total_chunks=total_chunks,
            total_pages=distinct_pages if distinct_pages else None,
            frontmatter_chunks=frontmatter_chunks,
        )

    def close(self) -> None:
        """关闭底层数据库连接（与索引的 close 同一语义）。"""
        with self._lock:
            self._conn.close()


def _split_subjects(subject: object | None) -> list[str]:
    """把 metadata 的 subject 字段拆成学科标签列表。

    ingest_books.py 把 manifest 的 subjects 列表以逗号连接写入 chunk
    metadata 的 subject 键（如 "机器学习,统计学习"，见该脚本注释），
    这里按逗号拆分并去空白；未注入时为 None/空 → 空列表。
    """
    if subject is None:
        return []
    return [item.strip() for item in str(subject).split(",") if item.strip()]


def merge_uploaded_catalog(
    catalog_documents: Iterable[KnowledgeDocumentInfo],
    uploaded: Iterable[KnowledgeDocumentInfo],
) -> list[KnowledgeDocumentInfo]:
    """把 API 上传文档并入词法库聚合结果（纯内存合并，不写库）。

    为什么需要合并（面向初学者）：上传文档在 _store_uploaded_document
    成功入库后已写入词法库，list_documents 的 SQL 聚合自然包含它——
    不需要合并。但调用方可能持有「上传回执级」的文档元数据（如
    上传刚完成、回执含 chunk_count），把它们并进来可让清单直接复用
    回执，避免重复查询。合并规则：以 document_id 为键去重，索引
    库聚合结果优先（它是权威数据源）；仅存在于 uploaded 的条目
    （理论上不会发生，防御性保留）追加在末尾。返回顺序稳定。
    """
    merged = {doc.document_id: doc for doc in catalog_documents}
    for doc in uploaded:
        merged.setdefault(doc.document_id, doc)
    return sorted(merged.values(), key=lambda doc: doc.document_id)


__all__ = [
    "KnowledgeBaseStats",
    "KnowledgeCatalog",
    "KnowledgeDocumentInfo",
    "NamespaceInfo",
    "SqliteKnowledgeCatalog",
    "merge_uploaded_catalog",
]
