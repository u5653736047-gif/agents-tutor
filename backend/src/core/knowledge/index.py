"""Replaceable index contract and a dependency-free in-memory implementation."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .models import Citation, KnowledgeChunk, SearchHit


@dataclass(frozen=True, slots=True)
class IngestMark:
    """ingest_marks 行的只读快照（版本管理比对用）。"""

    chunk_count: int
    page_count: int
    completed_at: str
    content_hash: str | None = None
    chunking: str | None = None


_ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+")
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]+")

# ── S5-C4：FTS5 候选预筛（词法路提速）───────────────────────────
# 设计见 SqliteKnowledgeIndex._setup_fts 与模块级纯函数注释。核心不变量：
# FTS 只圈定候选集合，打分/排序/平局规则仍在 Python 侧原样执行——候选集
# 相对「打分 > 0」集合必须是超集（等价性的前提）。
_LOGGER = logging.getLogger(__name__)
_FTS_TERM_LIMIT = 64
# S5-C4：候选数选择性路由阈值——MATCH 的 COUNT 探测成本远低于物化
# 全部候选行；低选择性查询（候选占比过高，如含常见单字的中文查询）
# 走全表扫描更便宜，路由后仅剩极小的 COUNT 开销。
_FTS_CANDIDATE_LIMIT = 2000
_CJK_RANGE = "\u4e00", "\u9fff"


def _is_cjk(char: str) -> bool:
    """单字符是否在 CJK 统一表意区（与 _CHINESE_RUN 同一范围）。"""
    return _CJK_RANGE[0] <= char <= _CJK_RANGE[1]


def _fts_transform(text: str) -> str:
    """把文本变换为「CJK 逐字空格分隔、字母数字词保持原样」的副本。

    为什么需要：FTS5 默认 unicode61 分词器把连续汉字并为单 token——
    「机器学习」入索引是一个整体 token，查单字「器」永远匹配不到。
    预变换后每个汉字都是独立 token，英文/数字词保持原样（unicode61 对
    [A-Za-z0-9]+ 的切分与 _ENGLISH_WORD 一致），查询侧同一变换后即可
    用短语精确对齐 bigram 的相邻语义（见 _fts_match_expression）。
    入库侧与查询侧必须使用同一函数——两侧不一致即破坏等价性。

    非 ASCII 拉丁字符（如 café/naïve/Über 中的 é/ï/Ü）按空格切分：
    _ENGLISH_WORD 仅捕获 [A-Za-z0-9]，而 FTS 的 unicode61 会把 café
    当单 token，二者不一致导致词项失配、FTS 候选集非超集。此处把
    非 ASCII 且非 CJK 的字母归一为空格，使索引 token 与打分侧切分对齐
    （café → "caf é"），保证 FTS 候选集仍是「打分 > 0」集合的超集。
    """
    out: list[str] = []
    for char in text:
        if _is_cjk(char):
            out.append(" ")
            out.append(char)
            out.append(" ")
        elif not char.isascii() and char.isalpha():
            # 非 ASCII 拉丁字母（如 é/ï/Ü）：按空格切分，对齐
            # _ENGLISH_WORD 的 ASCII 切分（见模块顶部 C4 决策）。
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def _fts_escape_token(token: str) -> str:
    """FTS5 字符串字面量转义：内部双引号加倍后整体加引号。

    引号包裹的 token 内部不含空格时等价于裸 token，含 FTS 语法字符
    （AND/OR/NOT/NEAR 等保留字、括号、运算符）时引号保证按字面处理。
    """
    escaped = token.replace('"', '""')
    return f'"{escaped}"'


def _fts_match_expression(terms: set[str]) -> str:
    """从词项集合构造 OR 连接的 FTS5 MATCH 表达式。

    词项分类（与 _lexical_terms 的产出规则对齐）：
    - 单个 CJK 字 → 裸 token（预变换后每字独立）；
    - 两个 CJK 字（bigram）→ 短语「c1 c2」（引号内空格分隔，精确对齐
      相邻语义——非相邻不命中）；
    - 英文/数字词 → 裸 token（小写已在 _lexical_terms 完成）。
    词项集为空时返回空串（search 入口对空词项集已提前返回 []，此处
    仅为防御——调用方不应把空串交给 MATCH）。
    """
    if not terms:
        return ""
    parts: list[str] = []
    for term in sorted(terms):  # 排序仅为了表达式确定性（测试友好）
        if len(term) == 2 and all(_is_cjk(char) for char in term):
            # 双字词项 → 单引号串短语「c1 c2」：短语要求相邻，与 bigram
            # 的相邻语义精确对齐（拆成两个引号 token 会变成隐式 AND，
            # 非相邻也命中，破坏超集性质）。
            parts.append(_fts_escape_token(f"{term[0]} {term[1]}"))
        else:
            parts.append(_fts_escape_token(term))
    expression = " OR ".join(parts)
    # 整体再包一层括号组：作为 AND 子条件与其他 WHERE 片段组合时不被
    # 隐式 AND 抢结合性。
    return f"({expression})" if len(parts) > 1 else expression


def _fts5_supported(conn: sqlite3.Connection) -> bool:
    """探测运行环境 SQLite 是否编译了 FTS5（极少见的缺失场景兜底用）。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            conn.execute("DROP TABLE IF EXISTS temp._fts5_probe")
        except sqlite3.OperationalError:
            pass
    return True
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
#
# namespace 保留键（S5-C1 决策 3，读时归一防御层）：
# - 键 "namespace" 缺失 ≡ 值 "public"（存量 chunk 无该键，语义上
#   全部属公共库）；两实现（InMemory/SQLite）对正向与排除两种语义
#   都按此归一，与回填迁移（主层，见 SqliteKnowledgeIndex 打开逻辑）
#   互为冗余防御；
# - 主层回填后所有行都有键，本归一只覆盖未经回填的数据路径
#   （外部灌库、旧库拷贝）。


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
            if key == "namespace" and meta_value is None:
                # S5-C1 决策 3（读时归一）：namespace 键缺失 ≡ "public"
                # ——存量 chunk 无该键，语义上全部属公共库。排除语义下
                # 同样成立：缺键 chunk 对 "!x" 通过（public ≠ x）、对
                # "!public" 被排除（它就是 public）。
                meta_value = "public"
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
        # S5-C1 决策 3（读时归一，SQLite 版）：namespace 键缺失 ≡
        # "public"——用 COALESCE 把缺键行的提取值归一为 public，正向
        # 与排除两个分支都基于归一后的值构建（与 InMemory 版同步）。
        value_expr = (
            f"COALESCE(json_extract(metadata_json, '$.{key}'), 'public')"
            if key == "namespace"
            else f"json_extract(metadata_json, '$.{key}')"
        )
        if value.startswith("!"):
            # H-T2 排除语义：wanted 是排除值，命中它即被剔除。
            wanted = value[1:]
            if key == "source":
                # source 是顶层列（非 JSON）：直接 != 比较。
                clauses.append("source != ?")
                params.append(wanted)
                continue
            # JSON1 三条件（语义见 docstring 第 3 点）：键不存在通过
            # （namespace 键经 COALESCE 归一为 public 后按值判定），
            # 值/列表不含排除值通过，等于/含排除值则被排除。
            clauses.append(
                f"({value_expr} IS NULL OR "
                f"({value_expr} != ? AND "
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
            f"({value_expr} = ? OR "
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
        # S5-C4：FTS5 可用性先置 False，_setup_fts 探测/建表后按实际
        # 结果置位——False 时 search 走既有全表扫描路径，零回归。
        self._fts_enabled = False
        self._create_tables()
        self._setup_fts()

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
            # S5-C3 版本管理增量迁移（沿用 sessions.py 的 PRAGMA 先例）：
            # 旧库的 ingest_marks 没有 content_hash/chunking 两列，补齐后
            # 「源文件变更检测」与「分块策略记录」才可用；幂等——列已存在
            # 时跳过。
            existing_mark_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(ingest_marks)")
            }
            for column in ("content_hash", "chunking"):
                if column not in existing_mark_columns:
                    self._conn.execute(
                        f"ALTER TABLE ingest_marks ADD COLUMN {column} TEXT"
                    )
            self._conn.commit()
            # S5-C1 决策 3（主层）：存量行幂等回填 namespace="public"。
            # 存量 chunk 无该键而正向过滤严格匹配——不回填则 C1 上线即
            # 全部隐身。json_set 只补缺失键，已有值（含非 public 空间）
            # 不动；重复打开零行受影响，幂等。
            self._conn.execute(
                "UPDATE chunks SET metadata_json = "
                "json_set(metadata_json, '$.namespace', 'public') "
                "WHERE json_extract(metadata_json, '$.namespace') IS NULL"
            )
            self._conn.commit()

    def _setup_fts(self) -> None:
        """S5-C4：探测并启用 FTS5 候选预筛（失败只告警回退，不阻断）。

        三道闸门任一不过即置 _fts_enabled=False 回退全表扫描：
        1. 环境无 FTS5 编译（极少见）；
        2. chunks_fts 表缺失（旧库未重建——不在启动期自动重建，大库
           重建会造成意外长启动阻塞；用 ingest_books.py --rebuild-fts）;
        3. 行数漂移（chunks 与 chunks_fts 行数不一致，说明同步写入被
           外部中断或库被外部改动）。
        """
        with self._lock:
            if not _fts5_supported(self._conn):
                _LOGGER.warning(
                    "FTS5 不可用：词法检索回退全表扫描（功能不受影响）"
                )
                return
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING "
                    "fts5(content, chunk_id UNINDEXED)"
                )
            except sqlite3.OperationalError as exc:
                _LOGGER.warning("FTS5 建表失败：%s；回退全表扫描", exc)
                return
            counts = self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM chunks), "
                "(SELECT COUNT(*) FROM chunks_fts)"
            ).fetchone()
            if counts[0] != counts[1]:
                _LOGGER.warning(
                    "FTS 表行数漂移（chunks=%s, chunks_fts=%s）："
                    "词法检索回退全表扫描；可用 ingest_books.py --rebuild-fts 重建",
                    counts[0],
                    counts[1],
                )
                return
            self._fts_enabled = True

    def rebuild_fts(self) -> int:
        """显式重建 FTS 预筛表（管理动作，非自动触发）。

        清空后从 chunks 表全量重写预变换副本。返回重建的行数；大库
        重建为秒级～十秒级线性操作（纯文本变换 + 批量插入），文档已
        写明预期成本。
        """
        with self._lock:
            if not _fts5_supported(self._conn):
                raise RuntimeError("当前 SQLite 环境不支持 FTS5，无法重建")
            self._conn.execute("DROP TABLE IF EXISTS chunks_fts")
            self._conn.execute(
                "CREATE VIRTUAL TABLE chunks_fts USING "
                "fts5(content, chunk_id UNINDEXED)"
            )
            rows = self._conn.execute(
                "SELECT chunk_id, content FROM chunks"
            ).fetchall()
            self._conn.executemany(
                "INSERT INTO chunks_fts(chunk_id, content) VALUES (?, ?)",
                [(chunk_id, _fts_transform(content)) for chunk_id, content in rows],
            )
            self._conn.commit()
            self._fts_enabled = True
            return len(rows)

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
            # S5-C4：FTS 预变换副本与 chunks 同事务写入（同一 commit），
            # 保证「检索候选集」与「打分语料」永不漂移。先删后插实现
            # 同 chunk_id 覆盖（FTS 无 UNINDEXED 主键语义）。
            if self._fts_enabled:
                fts_ids = [(row[0],) for row in rows]
                self._conn.executemany(
                    "DELETE FROM chunks_fts WHERE chunk_id = ?", fts_ids
                )
                self._conn.executemany(
                    "INSERT INTO chunks_fts(chunk_id, content) VALUES (?, ?)",
                    [
                        (row[0], _fts_transform(row[2]))
                        for row in rows
                    ],
                )
            self._conn.commit()

    def delete_document(self, document_id: str) -> None:
        """删除某个 document_id 的全部 chunk（整文档替换语义的删除半段）。"""
        with self._lock:
            # S5-C4：FTS 行与 chunks 同事务同步删除——先按 document_id
            # 圈出待删 chunk_id（删 chunks 行之前），再删 FTS 对应行。
            if self._fts_enabled:
                fts_ids = [
                    row[0]
                    for row in self._conn.execute(
                        "SELECT chunk_id FROM chunks WHERE document_id = ?",
                        (document_id,),
                    ).fetchall()
                ]
                self._conn.executemany(
                    "DELETE FROM chunks_fts WHERE chunk_id = ?",
                    [(chunk_id,) for chunk_id in fts_ids],
                )
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
            rows = self._fetch_search_rows(query, query_terms, normalized)

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

    def _fetch_search_rows(
        self,
        query: str,
        query_terms: set[str],
        normalized: dict[str, str],
    ) -> list[Any]:
        """取出参与打分的 chunk 行：FTS 候选预筛或全表扫描。

        S5-C4 路由规则（按序）：
        1. FTS 未启用（环境不支持 / 表缺失 / 行数漂移）→ 全表扫描；
        2. 词项数超过 _FTS_TERM_LIMIT → 全表扫描（截断 MATCH 会静默
           破坏超集性质，见计划决策 3）；
        3. FTS 可用 → MATCH 圈定候选 chunk_id，再取回候选行打分；
           候选数超过 _FTS_CANDIDATE_PARAM_LIMIT → 全表扫描（IN 参数
           数上限 + 区分度过低的保守信号）。
        返回行形态与全表扫描完全一致，下游打分/排序/截断零感知。
        """
        if (
            not self._fts_enabled
            or len(query_terms) > _FTS_TERM_LIMIT
        ):
            return self._full_scan_rows(normalized)
        expression = _fts_match_expression(query_terms)
        with self._lock:
            # 选择性路由：COUNT 只走 FTS 索引（不物化行），毫秒级；
            # 候选过多说明查询含高频单字（中文查询常态），物化全部候选
            # 再逐行打分并不比全表扫描便宜——此时直接回退，把开销压到
            # 一次 COUNT。等价性不受影响（两条路径返回相同结果集）。
            candidate_count = self._conn.execute(
                "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                (expression,),
            ).fetchone()[0]
            if candidate_count > _FTS_CANDIDATE_LIMIT:
                return self._full_scan_rows(normalized)
        with self._lock:
            # JOIN 在 SQLite 内部（C 速度）完成：MATCH 圈定候选后按
            # chunk_id 主键回表，无 Python 端参数列表（低区分度查询命中
            # 数万候选也不会触发变量数上限）。metadata 过滤以 AND 追加，
            # 裸列名解析到 chunks 表。
            sql = (
                "SELECT chunks.chunk_id, chunks.document_id, chunks.content, "
                "chunks.source, chunks.page, chunks.start, chunks.end, "
                "chunks.metadata_json "
                "FROM chunks JOIN chunks_fts ON chunks.chunk_id = chunks_fts.chunk_id "
                "WHERE chunks_fts MATCH ?"
            )
            params: list[object] = [expression]
            if normalized:
                where, filter_params = _metadata_where_clause(normalized)
                sql += f" AND ({where})"
                params.extend(filter_params)
            return self._conn.execute(sql, params).fetchall()

    def _full_scan_rows(self, normalized: dict[str, str]) -> list[Any]:
        """既有全表扫描路径原样保留（FTS 回退时的唯一数据来源）。"""
        with self._lock:
            if normalized:
                where, params = _metadata_where_clause(normalized)
                return self._conn.execute(
                    "SELECT chunk_id, document_id, content, source, page, start, end, "
                    f"metadata_json FROM chunks WHERE {where}",
                    params,
                ).fetchall()
            return self._conn.execute(
                "SELECT chunk_id, document_id, content, source, page, start, end, "
                "metadata_json FROM chunks"
            ).fetchall()

    # ── 入库完成标记（供批量入库脚本实现「已入库跳过 / 失败续跑」）──

    def is_document_complete(self, document_id: str) -> bool:
        """该 document_id 是否已有「整本入库成功」的完成标记。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ingest_marks WHERE document_id = ?", (document_id,)
            ).fetchone()
        return row is not None

    def document_mark(self, document_id: str) -> IngestMark | None:
        """读取完成标记内容（S5-C3 版本管理：--check-updates 比对用）。

        无标记返回 None；content_hash/chunking 在旧库迁移前列可为 None。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT chunk_count, page_count, completed_at, content_hash, chunking "
                "FROM ingest_marks WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        return IngestMark(
            chunk_count=row[0],
            page_count=row[1],
            completed_at=row[2],
            content_hash=row[3],
            chunking=row[4],
        )

    def mark_document_complete(
        self,
        document_id: str,
        *,
        chunk_count: int,
        page_count: int,
        content_hash: str | None = None,
        chunking: str | None = None,
    ) -> None:
        """写入完成标记（幂等：重复调用直接覆盖旧标记）。

        content_hash/chunking（S5-C3 版本管理）：源文件 sha256 与入库时
        分块策略，供 --check-updates 检测内容变更与策略漂移；None 时落
        NULL（兼容既有调用方与旧数据）。
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ingest_marks "
                "(document_id, chunk_count, page_count, completed_at, content_hash, chunking) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    chunk_count,
                    page_count,
                    datetime.now(UTC).isoformat(),
                    content_hash,
                    chunking,
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
