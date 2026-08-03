"""Knowledge-base data contracts shared by loaders, indexes, and tools.

metadata 领域字段约定（S3-T3，面向初学者）
------------------------------------------
KnowledgeDocument / KnowledgeChunk 的 metadata 是自由字典，但领域字段
有固定命名约定，来源分两类（不做模型自动标注）：

1. 清单注入（ingest 时由 scripts/ingest_books.py 写入，值来自
   knowledge_manifest.json）：
   - "subject": 学科标签，多学科用逗号连接（如 "机器学习,统计学习"）；
   - "difficulty": 难度枚举 beginner/intermediate/advanced；
   - "title": 书名。
2. 规则提取（分块时由 chunking.py 从标题行解析，见该模块注释）：
   - "chapter": 章节层级（如 "第1章"、"第三章"）；
   - "section": 小节编号（如 "3.2.1"）；
   - "tags": 概念标签（标题行核心词，最小可用启发式，字符串列表）。
3. 分块策略标记（仅语义分块存在）：
   - "chunking": "semantic"（S3-T2 起）。

检索过滤（service/index 的 metadata_filter 参数）按上述键名匹配，
另有特殊键 "source" 匹配 chunk 顶层的逻辑来源字段（限定某本书）。
"""

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
