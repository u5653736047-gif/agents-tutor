"""向量索引（S3-T4）：实现 KnowledgeIndex 协议的语义检索实现。

设计说明（按功能模块，面向初学者）：

1. 与词法索引的并存关系
   词法索引（index.py 的 InMemory/Sqlite 词法实现）按「字符命中」打分，
   查「土豆」永远找不到只写「马铃薯」的分块；向量索引按「语义相似度」
   打分，查询与分块先各自变成向量，再算相似度。两者都实现同一个
   `KnowledgeIndex` 协议，是「并存」的两套索引，不是替换关系：
   - 词法：确定性、可解释、零依赖，S3-T1 起的默认路径保持不变；
   - 向量：能命中同义表述，但依赖 Embedding 提供方，S3-T4 起可选接入
     （ingest 脚本 --vector 开关）。
   KnowledgeService 只依赖协议，构造时传入哪个索引就走向量还是词法
   （协议替换点；混合检索在 S3-T5 做融合，本任务不涉及）。

2. 检索原理：归一化 + 余弦相似度
   相似度计算（面向初学者）：
   - 查询文本和每个分块分别通过 EmbeddingProvider 变成等长向量；
   - L2 归一化：把向量每个分量除以向量长度，使向量长度变为 1
     （方向不变）。归一化后「余弦相似度 = 点积」——这是最常用的
     文本相似度度量，范围 [-1, 1]，1 表示方向完全一致；
   - 按相似度从高到低排序取 top_k；相似度 ≤ 0 视为不相关跳过
     （与词法索引「分数 ≤ 0 跳过」的约定一致，也满足
     SearchHit.score 的 ge=0 约束）。
   向量在哪一步归一化：入库（upsert）时把每个分块向量归一化后
   持久化；查询时只归一化查询向量。这样检索时只剩点积，省去
   每次重算分块向量长度。

3. 持久化方案：SQLite 表存 BLOB + 内存矩阵
   选型（书面结论详见 docs/EMBEDDING_SELECTION.md）：
   - 向量以 float32 二进制（struct 打包）存入 SQLite 的 BLOB 列，
     比 JSON 文本省约 5 倍空间（256 维 ≈ 1KB/条）；
   - 检索在内存中进行：构造时把整库向量一次性读入内存
     （1.5 万 chunk × 256 维 × 4 字节 ≈ 15MB，完全可承受），
     SQLite 只负责持久化与重载——SQLite 本身不适合做向量距离
     计算（全表扫描 + Python 解包比内存矩阵慢一个数量级）；
   - 为什么不用 Chroma：重依赖（chromadb 会连带安装 onnxruntime 等
     二进制包，Windows 兼容与锁文件都增加风险），本项目 1.5 万 chunk
     规模用自研方案足够；规模再大时可换 numpy 矩阵或专用向量库，
     协议与表结构不变（升级路径见选型文档）。

4. metadata 过滤复用
   向量检索同样支持 S3-T3 的 metadata_filter（限定书/难度/章节等），
   直接复用 index.py 的校验与匹配函数（_validate_metadata_filter /
   _matches_metadata_filter），保证与词法索引的过滤语义完全一致
   （先过滤、后打分排序、最后 top_k 截断），不另写一份过滤逻辑。
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import threading
from collections.abc import Iterable
from pathlib import Path

from .embedding import EmbeddingProvider
from .index import _matches_metadata_filter, _validate_metadata_filter
from .models import Citation, KnowledgeChunk, SearchHit


def _normalize(vector: list[float]) -> list[float]:
    """L2 归一化：每个分量除以向量长度，使向量长度为 1。

    归一化后余弦相似度 = 点积（见模块注释第 2 节）。零向量（如空文本
    产生的全 0 向量）长度本来就是 0，保持全 0 返回——它的点积恒为 0，
    会在打分时被当作不相关跳过。
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return [0.0] * len(vector)
    return [value / norm for value in vector]


def _dot(left: list[float], right: list[float]) -> float:
    """点积（两向量均已归一化，故点积即余弦相似度）。

    防御性长度断言（面向初学者）：zip 遇到长度不同的列表会静默截断到
    较短长度——如果查询向量与分块向量维度不一致（例如换了不同维度的
    embedding provider 却没有重建向量库），截断计算会给出错误的相似度。
    这里直接抛错，把配置错误暴露出来而不是静默算错。

    纯 Python 实现，零依赖：1.5 万 chunk × 256 维的检索约零点几秒，
    教学系统可接受。若未来规模变大，可把这里换成 numpy 的矩阵乘法
    （numpy @ 向量），检索函数与存储结构都不需要改。
    """
    if len(left) != len(right):
        raise ValueError(
            f"向量长度不一致：{len(left)} vs {len(right)}——"
            "请检查是否更换过不同维度的 embedding provider"
            "（更换后需 --force 重建向量库）"
        )
    return math.fsum(a * b for a, b in zip(left, right))


def _validate_embedding_output(
    vectors: list[list[float]], chunk_count: int, dimension: int
) -> None:
    """校验 Embedding 提供方输出：数量与维度都必须对得上。

    尽早失败原则（面向初学者）：向量数量/维度不一致说明提供方实现
    有 bug 或换过提供方（维度不同），此时继续计算会得到错误的相似度，
    甚至让持久化表里混入不同维度的向量——所以在写入前直接抛错。
    """
    if len(vectors) != chunk_count:
        raise ValueError(
            "embedding provider 返回的向量数量与输入文本数量不一致"
        )
    for vector in vectors:
        if len(vector) != dimension:
            raise ValueError(
                f"embedding provider 返回的向量维度 {len(vector)} "
                f"与声明的 dimension {dimension} 不一致"
            )


def _rank_hits(
    chunks: dict[str, KnowledgeChunk],
    vectors: dict[str, list[float]],
    query_vector: list[float],
    top_k: int,
    metadata_filter: dict[str, str],
) -> list[SearchHit]:
    """共享的检索打分流程：过滤 → 点积打分 → 排序 → top_k 截断。

    语义与词法索引完全对齐（模块注释第 4 节）：metadata_filter 非空时
    先剔除不匹配分块，剩下的按相似度降序排序（同分按 chunk_id 升序，
    与词法索引的平局规则一致），最后截断 top_k。
    """
    scored: list[tuple[float, str, KnowledgeChunk]] = []
    for chunk_id, chunk in chunks.items():
        if metadata_filter and not _matches_metadata_filter(chunk, metadata_filter):
            continue
        # 分块向量在 upsert 时已归一化，查询向量在 search 入口已归一化，
        # 所以这里直接点积就是余弦相似度。
        score = _dot(vectors[chunk_id], query_vector)
        if score <= 0.0:
            continue
        scored.append((score, chunk_id, chunk))
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


class InMemoryVectorKnowledgeIndex:
    """内存向量索引：EmbeddingProvider + 余弦相似度排序（仅测试/单线程使用）。

    无锁且不持久化：生产装配用 SqliteVectorKnowledgeIndex；若未来流入
    并发路径需加锁（对齐 SqliteVectorKnowledgeIndex 的 RLock 模式）。
    用法（面向初学者）：构造时传入一个 EmbeddingProvider（协议，
    可替换——测试注入替身，生产注入 HashEmbeddingProvider 或
    FastEmbedProvider），之后与词法索引的用法完全一致：
    upsert / delete_document / search(metadata_filter=...)。
    """

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._vectors: dict[str, list[float]] = {}

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        """插入分块：批量 embed + 归一化后入内存，同 chunk_id 覆盖。"""
        chunk_list = list(chunks)
        vectors = self._provider.embed([chunk.content for chunk in chunk_list])
        _validate_embedding_output(
            vectors, len(chunk_list), self._provider.dimension
        )
        for chunk, vector in zip(chunk_list, vectors):
            # 存归一化后的向量：检索时点积即余弦（见模块注释第 2 节）。
            self._chunks[chunk.chunk_id] = chunk
            self._vectors[chunk.chunk_id] = _normalize(vector)

    def delete_document(self, document_id: str) -> None:
        """删除某个 document_id 的全部分块（整文档替换语义的删除半段）。"""
        self._chunks = {
            chunk_id: chunk
            for chunk_id, chunk in self._chunks.items()
            if chunk.document_id != document_id
        }
        self._vectors = {
            chunk_id: vector
            for chunk_id, vector in self._vectors.items()
            if chunk_id in self._chunks
        }

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """向量检索：query 归一化后与全部分块做余弦排序（含过滤）。"""
        if not query.strip() or top_k <= 0:
            return []
        query_vector = _normalize(self._provider.embed([query])[0])
        return _rank_hits(
            self._chunks,
            self._vectors,
            query_vector,
            top_k,
            _validate_metadata_filter(metadata_filter),
        )


class SqliteVectorKnowledgeIndex:
    """SQLite 持久化向量索引：向量存 BLOB，检索在内存矩阵进行。

    与词法 SqliteKnowledgeIndex 的关系（面向初学者）：这是另一套
    独立的数据库文件（默认 data/vector_knowledge.db，与词法库
    data/knowledge.db 并列），表结构在词法 chunks 表基础上多一列
    vector BLOB。两份数据由 ingest 脚本同步维护（--vector 开关），
    检索各自独立；S3-T5 混合检索时在服务层融合两路结果。
    """

    def __init__(self, db_path: str | Path, provider: EmbeddingProvider) -> None:
        # 线程安全说明（与词法 SqliteKnowledgeIndex 完全一致，见 index.py）：
        # 1. 索引在 FastAPI lifespan（主线程）创建，工具调用在工作线程池
        #    执行——必须 check_same_thread=False，否则跨线程使用直接抛
        #    ProgrammingError（T2 冒烟即因此全部 tool_execution_failed）；
        #    与 core/persistence.py 的 checkpointer 先例保持一致。
        # 2. check_same_thread=False 只是「允许」跨线程，连接本身不是线程
        #    安全的，必须用 RLock 串行化所有 self._conn 访问。
        # 3. 本类除了连接还有共享状态：内存矩阵 self._chunks/_vectors
        #    （search 读、upsert/delete 写）。锁的粒度是「共享状态访问」
        #    ——连接操作与内存矩阵更新在同一临界区内，保证 upsert 返回后
        #    search 立刻看到新数据；embedding、打分排序等纯计算在锁外。
        #    RLock 可重入，方法间互调（__init__ → _create_tables/_load_all）
        #    不会死锁。
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            # WAL 模式与词法库一致：读写并发更友好。
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._provider = provider
        self._chunks: dict[str, KnowledgeChunk] = {}
        self._vectors: dict[str, list[float]] = {}
        try:
            # 建表与加载同属初始化阶段，统一纳入 try：任一失败都在
            # 下面关闭连接再重抛（防御不对称——建表失败同样会泄漏
            # 连接，处理与 _load_all 失败时完全一致）。
            self._create_tables()
            self._load_all()
        except Exception:
            # 建表或加载失败（如维度校验不过）时关闭连接再重抛，避免连接泄漏。
            with self._lock:
                self._conn.close()
            raise

    def _create_tables(self) -> None:
        """建表：chunk_vectors 在词法 chunks 表结构上加 vector BLOB 列。"""
        # 访问 self._conn，加锁串行化（原因见 __init__ 线程安全说明）。
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunk_vectors (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    page INTEGER,
                    start INTEGER NOT NULL,
                    end INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    vector BLOB NOT NULL
                )
                """
            )
            self._conn.commit()

    def _load_all(self) -> None:
        """构造时把整库读入内存：检索在内存矩阵上进行（见模块注释第 3 节）。

        维度守卫（防御换 provider 后重载旧库）：向量库是上次入库时用
        当时的 provider 维度写入的；如果现在换了一个维度不同的 provider
        （如哈希替身 256 维 → fastembed 512 维），旧库向量与查询向量
        长度不一致，点积会被 _dot 拒绝而不是静默截断。此时必须 --force
        重新入库重建向量库（选型文档与 FastEmbedProvider 注释均有说明）。
        """
        # 锁内完成整库读取 + 内存矩阵填充：连接访问与内存矩阵更新必须
        # 在同一临界区（构造阶段无并发，加锁是为了与 upsert/delete 的
        # 更新路径保持同一模式，防止未来重载路径并发调用）。
        with self._lock:
            rows = self._conn.execute(
                "SELECT chunk_id, document_id, content, source, page, start, end, "
                "metadata_json, vector FROM chunk_vectors"
            )
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
                    vector_blob,
                ) = row
                vector = _unpack_vector(vector_blob)
                if len(vector) != self._provider.dimension:
                    raise ValueError(
                        f"向量库维度 {len(vector)} 与当前 embedding provider 的 "
                        f"dimension {self._provider.dimension} 不一致：请用 --force "
                        "重新入库重建向量库（更换 embedding provider 后维度可能变化）"
                    )
                self._chunks[chunk_id] = KnowledgeChunk(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    content=content,
                    source=source,
                    page=page,
                    start=start,
                    end=end,
                    metadata=json.loads(metadata_json),
                )
                self._vectors[chunk_id] = vector

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        """插入分块：批量 embed → 归一化 → 写 BLOB 并同步内存，同 ID 覆盖。"""
        # embed 与行数据构造是纯计算（不碰共享状态），放锁外；
        # 锁内完成 SQL 写 + 内存矩阵更新，保证返回后立刻可检索。
        chunk_list = list(chunks)
        contents = [chunk.content for chunk in chunk_list]
        vectors = self._provider.embed(contents)
        _validate_embedding_output(vectors, len(contents), self._provider.dimension)
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
                _pack_vector(_normalize(vector)),
            )
            for chunk, vector in zip(chunk_list, vectors)
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO chunk_vectors "
                "(chunk_id, document_id, content, source, page, start, end, "
                "metadata_json, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            # 同步内存矩阵（与 SQL 同一临界区）：写入后立刻可检索
            # （不重新读库），且不会让并发 search 看到半更新状态。
            for chunk, vector in zip(chunk_list, vectors):
                self._chunks[chunk.chunk_id] = chunk
                self._vectors[chunk.chunk_id] = _normalize(vector)

    def delete_document(self, document_id: str) -> None:
        """删除某个 document_id 的全部向量分块（SQL + 内存同步删除）。"""
        # SQL 删除与内存矩阵删除在同一临界区，避免 search 读到不一致状态。
        with self._lock:
            self._conn.execute(
                "DELETE FROM chunk_vectors WHERE document_id = ?", (document_id,)
            )
            self._conn.commit()
            self._chunks = {
                chunk_id: chunk
                for chunk_id, chunk in self._chunks.items()
                if chunk.document_id != document_id
            }
            self._vectors = {
                chunk_id: vector
                for chunk_id, vector in self._vectors.items()
                if chunk_id in self._chunks
            }

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """向量检索：与 InMemory 版共用同一套打分流程（内存矩阵）。"""
        if not query.strip() or top_k <= 0:
            return []
        query_vector = _normalize(self._provider.embed([query])[0])
        # 锁内浅拷贝内存矩阵快照后立即释放锁，打分在锁外：既防止并发
        # upsert/delete 修改字典导致迭代崩溃/读到半更新状态，又不持锁
        # 做全库打分循环（1.5 万条浅拷贝仅毫秒级，打分才是大头）。
        with self._lock:
            chunks = dict(self._chunks)
            vectors = dict(self._vectors)
        return _rank_hits(
            chunks,
            vectors,
            query_vector,
            top_k,
            _validate_metadata_filter(metadata_filter),
        )

    def has_document(self, document_id: str) -> bool:
        """该 document_id 在向量库中是否已有分块。

        供 ingest 脚本判断「词法已入库但向量缺失」的增量补建场景
        （详见 ingest_books.py 的 --vector 说明）。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM chunk_vectors WHERE document_id = ? LIMIT 1",
                (document_id,),
            ).fetchone()
        return row is not None

    def close(self) -> None:
        """关闭底层数据库连接（进程退出前调用，与词法库用法一致）。"""
        with self._lock:
            self._conn.close()


def _pack_vector(vector: list[float]) -> bytes:
    """把 float 列表打包成二进制 BLOB（float32 小端，约 1KB/256 维）。

    为什么用二进制而不是 JSON 文本（面向初学者）：BLOB 体积约为
    JSON 的 1/5，1.5 万条向量省下几十 MB 磁盘与加载时间；struct 是
    标准库，零新增依赖。
    """
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    """把 BLOB 解包回 float 列表（维度由字节数推断，见 _pack_vector）。"""
    if len(blob) % 4 != 0:
        raise ValueError("vector BLOB 长度不是 float32 的整数倍，数据可能损坏")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


__all__ = [
    "InMemoryVectorKnowledgeIndex",
    "SqliteVectorKnowledgeIndex",
]
