"""Replaceable index contract and a dependency-free in-memory implementation."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
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
#
# 否定/排除语义（H-T2，向量噪音治理）：
# - 值以 "!" 开头表示「排除」：该键值（字符串精确相等，或列表任一
#   元素相等）等于 "!" 后内容时，该 chunk 被排除；键不存在视为
#   「不匹配该排除条件」，因此通过。普通值（不以 ! 开头）语义不变。
# - 约定："!" 前缀是保留字，领域值不应以 ! 开头（如 subject 等
#   领域字段的值不会真的以 ! 开头）。
# - 典型用法：检索侧默认抑制前言/目录类噪音 chunk——service 层自动
#   合并 {"chunk_class": "!frontmatter"}（见 service.py 的
#   suppress_frontmatter），词法/向量/混合三路语义一致。


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

    H-T2 否定语义：值以 "!" 开头表示排除（见模块顶部契约注释）——
    命中排除值（字符串相等或列表任一元素相等）时该 chunk 不匹配；
    键不存在视为「不匹配该排除条件」，因此通过。
    """
    for key, value in metadata_filter.items():
        exclude = value.startswith("!")
        wanted = value[1:] if exclude else value
        if key == "source":
            matched = chunk.source == wanted
        else:
            meta_value = chunk.metadata.get(key)
            if isinstance(meta_value, list):
                matched = any(str(item) == wanted for item in meta_value)
            elif meta_value is None:
                matched = False
            else:
                matched = str(meta_value) == wanted
        if exclude:
            if matched:
                return False  # 命中排除值 → 不匹配（被过滤掉）
        elif not matched:
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
    3. 排除语义（H-T2，值以 "!" 开头，见模块顶部契约注释）：
       - source 排除：顶层列直接 source != ?；
       - 普通键排除：JSON1 三条件——键不存在（json_extract 为
         NULL）通过；值存在但既不等于排除值、列表也不含排除值时
         通过；值等于排除值或列表含排除值时排除（三条件合起来
         与 InMemory 版语义一致，键不存在不被误排除）。
    4. 不用独立列：metadata 键集会随领域字段扩展（S3-T3 已 7 个键），
       独立列需要 ALTER TABLE 迁移旧库且键集固定；向量不进 metadata——
       S3-T4 起向量由向量索引的独立 BLOB 列存储（见 vector_index.py）。
       JSON1 是 Python 内置 sqlite3 自带能力，无需迁移。数万级 chunk
       的过滤开销可控（全表扫描打分本就要读每一行）。
    5. 防注入：键名已通过 _METADATA_KEY_PATTERN 白名单校验，拼进
       JSON path 安全；值一律用绑定参数。
    """
    clauses: list[str] = []
    params: list[object] = []
    for key, value in metadata_filter.items():
        if value.startswith("!"):
            # H-T2 排除语义：wanted 是排除值，命中它即被剔除。
            wanted = value[1:]
            if key == "source":
                # source 是顶层列（非 JSON）：直接 != 比较。
                clauses.append("source != ?")
                params.append(wanted)
                continue
            # JSON1 三条件（语义见 docstring 第 3 点）：键不存在通过，
            # 值/列表不含排除值通过，等于/含排除值则被排除。
            clauses.append(
                f"(json_extract(metadata_json, '$.{key}') IS NULL OR "
                f"(json_extract(metadata_json, '$.{key}') != ? AND "
                f"NOT EXISTS (SELECT 1 FROM json_each(metadata_json, '$.{key}') "
                "WHERE json_each.value = ?)))"
            )
            params.append(wanted)
            params.append(wanted)
            continue
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


# 可替换的索引契约：未来向量索引只需实现 upsert / delete_document / search
# 三个方法即可接入检索链路，调用方不关心底层是内存、SQLite 还是向量库。
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

    def chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """Fetch one chunk by ID (I2：查看原文 / 分块回溯的读接口）。

        返回 None 表示不存在（约定与 delete_document 的幂等删除
        一致：调用方不做存在性判断，读接口用 None 表达未命中）。
        实现说明：可选能力——混合索引 / 服务层依赖它做「引用回溯」，
        纯检索场景可以抛 NotImplementedError 或不实现（鸭子类型
        不强制）；Sqlite / InMemory 必须实现（它们是底线索引）。
        """
        ...


class InMemoryKnowledgeIndex:
    """内存词法索引：仅测试/单线程使用（生产装配用 SqliteKnowledgeIndex）。

    无锁且不持久化：并发 upsert/search 会产生竞态；若未来流入并发路径
    需加锁（对齐 SqliteKnowledgeIndex 的 RLock 模式）。
    """

    def __init__(self) -> None:
        self._chunks: dict[str, KnowledgeChunk] = {}

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        # 同 chunk_id 的旧分块直接覆盖（整文档替换入库时旧版残片不会残留）。
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk

    def delete_document(self, document_id: str) -> None:
        # 重建字典：只保留不属于该文档的 chunk。
        self._chunks = {
            chunk_id: chunk
            for chunk_id, chunk in self._chunks.items()
            if chunk.document_id != document_id
        }

    def chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """按 chunk_id 取回分块（I2）：不存在返回 None（dict 查询）。"""
        return self._chunks.get(chunk_id)

    def chunks_of_document(self, document_id: str) -> list[KnowledgeChunk]:
        """读取某文档的全部分块（I2 浏览）：按 (start, chunk_id) 排序。

        与 SqliteKnowledgeIndex 的 chunks_of_document 同一顺序约定
        （与入库顺序一致），保证多次读取顺序稳定、跨实现行为一致。
        """
        return sorted(
            (
                chunk
                for chunk in self._chunks.values()
                if chunk.document_id == document_id
            ),
            key=lambda chunk: (chunk.start, chunk.chunk_id),
        )

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """词法检索：先按 metadata_filter 过滤，再打分、排序、截断 top_k。"""
        if not query.strip() or top_k <= 0:  # 空查询或非法 top_k 直接返回空
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

        # 分数降序，同分按 chunk_id 排序保证输出顺序稳定。
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
        # 线程安全说明（为什么 check_same_thread=False + 为什么还要 RLock）：
        # 1. 索引在 FastAPI lifespan（主线程）里创建，而 graph.run 的工具
        #    调用跑在 FastAPI 工作线程池（run_in_threadpool）——SQLite 默认
        #    拒绝跨线程使用连接（check_same_thread=True），工作线程一调用
        #    就抛 ProgrammingError（T2 冒烟因此全部 tool_execution_failed）。
        #    与 core/persistence.py 的 checkpointer 先例保持一致：允许跨线程。
        # 2. check_same_thread=False 只是「允许」跨线程，连接本身仍然不是
        #    线程安全的：两个线程同时 execute 同一连接会数据错乱甚至崩溃，
        #    所以所有访问 self._conn 的操作必须用 RLock 串行化。
        # 3. 锁的粒度：只锁「共享状态（连接）访问」部分——游标创建 +
        #    execute + fetch 的完整序列，纯计算（打分排序）在锁外做，
        #    避免长持有锁；RLock 可重入，方法间互调（如 __init__ →
        #    _create_tables）不会死锁。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            # WAL 模式下读操作不阻塞写操作，对脚本与后续检索并发更友好。
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        # 访问 self._conn，加锁串行化（原因见 __init__ 的线程安全说明）。
        with self._lock:
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
        # 行数据构造是纯计算（不碰连接），放锁外；锁内只做
        # execute + commit 的完整写序列（原因见 __init__ 线程安全说明）。
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
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO chunks "
                "(chunk_id, document_id, content, source, page, start, end, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def delete_document(self, document_id: str) -> None:
        """删除某个 document_id 的全部 chunk（整文档替换语义的删除半段）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM chunks WHERE document_id = ?", (document_id,)
            )
            self._conn.commit()

    def chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """按 chunk_id 取回分块（I2）：不存在返回 None。

        与 upsert / chunks_of_document 同一行结构反序列化；锁内
        execute + fetchone 取快照（同一连接不容并发操作）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT chunk_id, document_id, content, source, page, start, end, "
                "metadata_json FROM chunks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
        if row is None:
            return None
        (
            stored_chunk_id,
            document_id,
            content,
            source,
            page,
            start,
            end,
            metadata_json,
        ) = row
        return KnowledgeChunk(
            chunk_id=stored_chunk_id,
            document_id=document_id,
            content=content,
            source=source,
            page=page,
            start=start,
            end=end,
            metadata=json.loads(metadata_json),
        )

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
        if not query.strip() or top_k <= 0:  # 空查询或非法 top_k 直接返回空
            return []

        query_terms = _lexical_terms(query)
        if not query_terms:
            return []

        normalized = _validate_metadata_filter(metadata_filter)
        # 锁内完成「游标创建 + execute + fetch」完整序列：fetchall 一次性
        # 取出数据快照后立即释放锁，打分排序在锁外进行——既保证同一连接
        # 不被并发操作（迭代途中别的线程写库会出错），又不持锁做长循环。
        with self._lock:
            if normalized:
                where, params = _metadata_where_clause(normalized)
                rows = self._conn.execute(
                    "SELECT chunk_id, document_id, content, source, page, start, end, "
                    f"metadata_json FROM chunks WHERE {where}",
                    params,
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT chunk_id, document_id, content, source, page, start, end, "
                    "metadata_json FROM chunks"
                ).fetchall()

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

        # 同内存版：分数降序，同分按 chunk_id 排序。
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            self._conn.execute(
                "DELETE FROM ingest_marks WHERE document_id = ?", (document_id,)
            )
            self._conn.commit()

    def chunks_of_document(self, document_id: str) -> list[KnowledgeChunk]:
        """读取某个 document_id 的全部分块（含 metadata 反序列化）。

        S3-T4 用途（面向初学者）：向量索引是独立于词法索引的另一份
        数据。当词法库已入库、向量库缺失时（例如先前入库没有带
        --vector），ingest 脚本用本方法把已有分块原样读出来补写向量
        索引，无需重新解析 PDF（详见 ingest_books.py 的 --vector 说明）。
        按 (start, chunk_id) 排序，保证多次读取顺序稳定。
        """
        # 锁内 execute + fetchall 取快照（同一连接不容并发操作），
        # 反序列化构造对象是纯计算，放锁外。
        with self._lock:
            rows = self._conn.execute(
                "SELECT chunk_id, document_id, content, source, page, start, end, "
                "metadata_json FROM chunks WHERE document_id = ? "
                "ORDER BY start, chunk_id",
                (document_id,),
            ).fetchall()
        return [
            KnowledgeChunk(
                chunk_id=chunk_id,
                document_id=stored_document_id,
                content=content,
                source=source,
                page=page,
                start=start,
                end=end,
                metadata=json.loads(metadata_json),
            )
            for (
                chunk_id,
                stored_document_id,
                content,
                source,
                page,
                start,
                end,
                metadata_json,
            ) in rows
        ]

    def close(self) -> None:
        """关闭底层数据库连接。"""
        with self._lock:
            self._conn.close()


def _lexical_terms(text: str) -> set[str]:
    """Extract lowercase English words plus Chinese characters and pairs."""
    # 英文/数字词统一转小写（检索时大小写不敏感）。
    terms = {match.group().lower() for match in _ENGLISH_WORD.finditer(text)}
    for match in _CHINESE_RUN.finditer(text):
        run = match.group()
        # 中文按「单字 + 相邻两字组合」拆词：两字词（如 "线性"）也能被命中。
        terms.update(run)
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


__all__ = ["InMemoryKnowledgeIndex", "KnowledgeIndex", "SqliteKnowledgeIndex"]
