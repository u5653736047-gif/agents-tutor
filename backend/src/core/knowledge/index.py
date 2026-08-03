"""Replaceable index contract and a dependency-free in-memory implementation."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .models import Citation, KnowledgeChunk, SearchHit

_ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+")
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")
# metadata_filter 键名白名单：只允许简单标识符（同时防 SQL 注入——
# 键名会拼进 json_each 的 JSON path，非法字符直接拒绝）。
_METADATA_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── metadata 过滤条件（S3-T3）─────────────────────────────────────
#
# 过滤约定（两实现语义必须完全一致，测试锁定）：
# - metadata_filter 是「键 → 字符串值」字典，多键之间是与（AND）关系，
#   所有条件同时满足的 chunk 才进入打分排序；
# - 特殊键 "source" 匹配 chunk 的顶层 source 字段（即「限定某本书」），
#   其余键匹配 chunk.metadata 里的同名键（subject/difficulty/chapter/
#   section/tags 等领域字段，约定见 models.py 模块注释）；
# - 值的匹配：metadata 值为字符串时精确相等；值为字符串列表时（如
#   tags）任一元素相等即匹配；键不存在视为不匹配；
# - 过滤发生在打分之前，top_k 截断发生在过滤与排序之后（先过滤，
#   后排序，最后截断）。


def _validate_metadata_filter(
    metadata_filter: Mapping[str, object] | None,
) -> dict[str, str]:
    """校验过滤条件：返回规范化副本。

    语义约定：
    - metadata_filter 为 None 表示「不过滤」，返回空字典；
    - 空字典 {} 等价于不过滤（走完整校验后自然返回空字典）；
    - 类型错误（不是 Mapping、值不是字符串）抛 TypeError；
      格式错误（键名非法，防 JSON path 注入）抛 ValueError。

    注解与实现一致：注解用 Mapping（协变，dict[str, str] 可传入），
    实现按 Mapping 判断——OrderedDict 等 Mapping 子类同样接受，
    str/list/int 等非 Mapping 一律拒绝（选实现兼容而非收窄注解，
    因为 dict 是不变类型，注解改 dict[str, object] 会让 mypy
    拒绝 dict[str, str] 实参）。
    """
    if metadata_filter is None:
        return {}
    if not isinstance(metadata_filter, Mapping):
        raise TypeError("metadata_filter must be a mapping")
    normalized: dict[str, str] = {}
    for key, value in metadata_filter.items():
        if not isinstance(key, str) or not _METADATA_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid metadata_filter key: {key!r}")
        if not isinstance(value, str):
            raise TypeError(f"metadata_filter value for {key!r} must be a string")
        normalized[key] = value
    return normalized


def _matches_metadata_filter(
    chunk: KnowledgeChunk, metadata_filter: dict[str, str]
) -> bool:
    """InMemory 版：单个 chunk 是否通过全部过滤条件。

    用 str() 统一比较以对齐 SQLite 的 = 语义（SQLite 对数字与文本
    做类型亲和转换，1 = '1' 成立；领域字段约定为字符串/字符串列表，
    这里只是防御性对齐）。注意：在该形态下两实现一致；bool 等非常规
    值行为可能不同——InMemory 的 str(True) == 'True'，而 SQLite 的
    1 = 'True' 不成立（bool 是 int 子类，SQLite 按数字比较）。
    """
    for key, value in metadata_filter.items():
        if key == "source":
            if chunk.source != value:
                return False
            continue
        meta_value = chunk.metadata.get(key)
        if isinstance(meta_value, list):
            if not any(str(item) == value for item in meta_value):
                return False
        elif meta_value is None or str(meta_value) != value:
            return False
    return True


def _metadata_where_clause(
    metadata_filter: dict[str, str],
) -> tuple[str, list[object]]:
    """SQLite 版：把过滤条件翻译成参数化 WHERE 片段。

    选型说明（为什么在 SQL 层用 JSON1 过滤，而不是取回后过滤、
    也不是把 metadata 拆成独立列）：
    1. SQL 层 WHERE：当前检索本来就是全表扫描打分（词法索引无倒排），
       在 WHERE 里提前剔除不匹配行不会增加额外开销，反而省掉被过滤
       行的 json.loads 与对象构造；语义上「先过滤后排序」也最直白。
    2. JSON1 双分支 OR（对每个过滤键）：
       - json_extract(metadata_json, '$.key') = ?：处理字符串值
         （学科/难度/章节等标量字段，精确相等）；
       - EXISTS (SELECT 1 FROM json_each(metadata_json, '$.key')
         WHERE json_each.value = ?)：处理字符串列表值（如 tags——
         json_each 遍历数组的每个子元素，任一元素相等即匹配）。
       两个分支 OR 组合后，str 与 list 两种值形态统一覆盖，键不存在
       时两分支都不成立 → 不匹配，与 InMemory 版语义一致。
       选 OR 而非只用 json_each 的原因：无论 json_each 在路径指向
       标量时返回 0 行还是 1 行（不同 SQLite 版本行为可能不同），
       OR 双分支的结果都正确——字符串值由 json_extract 分支命中，
       列表值由 json_each 分支命中，两者互不依赖。
    3. 不用独立列：metadata 键集会随领域字段扩展（S3-T3 已 7 个键，
       S3-T4 还会加 embedding 相关字段），独立列需要 ALTER TABLE
       迁移旧库且键集固定；JSON1 是 Python 内置 sqlite3 自带能力，
       无需迁移。数万级 chunk 的过滤开销可控（全表扫描打分本就要
       读每一行）。
    4. 防注入：键名已通过 _METADATA_KEY_PATTERN 白名单校验，拼进
       JSON path 安全；值一律用绑定参数。
    """
    clauses: list[str] = []
    params: list[object] = []
    for key, value in metadata_filter.items():
        if key == "source":
            # source 是顶层列（非 JSON）：直接比较，可走普通索引。
            clauses.append("source = ?")
            params.append(value)
            continue
        clauses.append(
            f"(json_extract(metadata_json, '$.{key}') = ? OR "
            f"EXISTS (SELECT 1 FROM json_each(metadata_json, '$.{key}') "
            "WHERE json_each.value = ?))"
        )
        params.append(value)
        params.append(value)
    return " AND ".join(clauses), params


class KnowledgeIndex(Protocol):
    """Small contract that future vector indexes can implement."""

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        """Insert chunks, replacing an existing chunk with the same ID."""
        ...

    def delete_document(self, document_id: str) -> None:
        """Delete every chunk belonging to a document."""
        ...

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """Return the highest-scoring chunks for a query.

        metadata_filter（S3-T3）：可选，先按条件过滤再打分排序，
        top_k 截断发生在过滤之后（约定见模块顶部注释）。
        """
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

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """词法检索：先按 metadata_filter 过滤，再打分、排序、截断 top_k。"""
        if not query.strip() or top_k <= 0:
            return []

        query_terms = _lexical_terms(query)
        if not query_terms:
            return []

        normalized = _validate_metadata_filter(metadata_filter)
        hits: list[SearchHit] = []
        for chunk in self._chunks.values():
            if normalized and not _matches_metadata_filter(chunk, normalized):
                continue
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

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """词法检索：过滤条件在 SQL 层 WHERE 生效，打分排序与内存版一致。

        过滤实现选型与语义详见 _metadata_where_clause 与模块顶部注释：
        WHERE 提前剔除不匹配行（JSON1 json_each），剩下的行在 Python
        侧打分、排序、截断 top_k——与 InMemoryKnowledgeIndex 完全一致。
        """
        if not query.strip() or top_k <= 0:
            return []

        query_terms = _lexical_terms(query)
        if not query_terms:
            return []

        normalized = _validate_metadata_filter(metadata_filter)
        if normalized:
            where, params = _metadata_where_clause(normalized)
            rows = self._conn.execute(
                "SELECT chunk_id, document_id, content, source, page, start, end, "
                f"metadata_json FROM chunks WHERE {where}",
                params,
            )
        else:
            rows = self._conn.execute(
                "SELECT chunk_id, document_id, content, source, page, start, end, "
                "metadata_json FROM chunks"
            )

        scored: list[tuple[float, str, KnowledgeChunk]] = []
        for row in rows:
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
