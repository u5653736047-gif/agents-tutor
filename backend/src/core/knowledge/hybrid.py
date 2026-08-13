"""S3-T5 混合检索索引：词法与向量两路结果按 RRF 融合排序。

（面向初学者的设计说明，按功能模块）

1. 为什么需要混合检索
   词法检索按「字符命中」打分，查「土豆」永远找不到只写「马铃薯」的
   分块（同义表述失效）；向量检索按「语义相似度」打分，能命中同义
   表述，但对「拼写相似、语义不同」的词对可能给出误导性分数，且依赖
   Embedding 提供方。两路各有盲区——把两路结果合并排序（混合检索），
   一路失效时另一路兜底。

2. 融合方案：RRF（Reciprocal Rank Fusion，倒数排名融合）
   两路分数的「量纲」不同：词法分数是命中词数（整数，如 3），向量
   分数是余弦相似度（0~1 的小数），直接相加没有意义。RRF 不看原始
   分数、只看「排名」：
       融合分(chunk) = Σ 1 / (k + 该 chunk 在某一路的排名)
   - k 是平滑常数，默认 60（文献经典取值，构造参数 rrf_k 可调）：
     第 1 名得 1/61 ≈ 0.0164，第 2 名得 1/62 ≈ 0.0161——差距很小，
     RRF 的哲学是「两路排名都靠前的 chunk 才真正相关」，而不是让
     某一路的高分独裁；
   - 只被一路命中的 chunk 得单项分（如 1/61），仍能进入结果——这
     正是「一路失效时另一路兜底」的机制；
   - 排序：融合分降序，同分按 chunk_id 升序（与两路各自的平局规则
     一致，保证结果确定、可复现）。
   为什么不用「归一化后加权求和」：归一化依赖每路分数的分布假设
   （词法分除以查询词数？余弦本身就在 [0,1]？），权重（0.5/0.5？）
   没有先验依据，调参敏感且难以解释；RRF 零参数调优、对量纲不敏感、
   完全确定，最适合教学系统。若未来想偏向某一路，给该路换更小的 k
   即可（本实现目前两路等权）。

3. 候选窗口（为什么两路各自多取一些再融合）
   融合只看「每一路的前 N 名」。若两路都只取 top_k 名再融合，一个
   「在词法路排第 1、在向量路排第 8」的 chunk（top_k=5 时向量路只
   截到前 5 名，看不到它）会丢掉向量路的加分。因此混合层让两路各自
   返回 max(top_k × 2, 10) 名（top_k=5 时窗口 10，第 8 名仍在窗口
   内），融合后再截断 top_k。注意：窗口只是「部分缓解」——排名在
   窗口之外的项仍会丢掉另一路的加分，但这是可控的取舍：两路各自
   多排几项的成本可忽略（本项目本来就是全表打分），换来融合结果
   对「另一路排名靠前」的项更完整。

4. metadata 过滤在融合之前生效（与 S3-T3 语义一致）
   metadata_filter 直接透传给两路，由两路各自「先过滤 → 再打分排序」
   ——被过滤掉的 chunk 不会进入任何一路的候选，自然也不会进入融合
   结果。混合层不重复实现过滤逻辑，而是复用两路已有的、被 S3-T3
   测试锁定的实现，保证过滤语义与词法/向量单路完全一致（过滤在打分
   排序前，top_k 截断在过滤后——融合层的 top_k 截断同样在过滤之后）。

5. 降级语义（词法单路保留为降级选项）
   - vector 参数为 None 时，本索引退化为纯词法：search 直接透传给
     词法路（分数、排序与词法单路逐项一致），upsert/delete_document
     只写词法路；
   - 自动降级的触发条件见 open_vector_index_if_available 的 docstring；
   - 判断当前是否在降级：读取 vector_enabled 属性（True = 双路，
     False = 纯词法降级）。

6. 默认路径接入点
   KnowledgeService 只依赖 KnowledgeIndex 协议：把本索引传给
   KnowledgeService，search_knowledge 工具即可默认走混合检索。
   scripts/ingest_books.py 的 --verify 分支（检索验证入口）已接入
   本索引作为默认；词法单路仍是显式可用的降级选项（vector 传 None）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .embedding import EmbeddingProvider, HashEmbeddingProvider
from .index import KnowledgeIndex
from .models import KnowledgeChunk, SearchHit
from .vector_index import SqliteVectorKnowledgeIndex


class HybridKnowledgeIndex:
    """词法 + 向量两路检索的融合索引（实现 KnowledgeIndex 协议）。

    参数（面向初学者）：
    - lexical：词法索引（InMemoryKnowledgeIndex 或 SqliteKnowledgeIndex），
      必选——它是永不降级的底线检索；
    - vector：向量索引（InMemoryVectorKnowledgeIndex 或
      SqliteVectorKnowledgeIndex），可选——None 表示降级为纯词法
      （降级语义见模块注释第 5 节）；
    - rrf_k：RRF 平滑常数（默认 60，经典取值，见模块注释第 2 节）。
    """

    def __init__(
        self,
        lexical: KnowledgeIndex,
        vector: KnowledgeIndex | None = None,
        *,
        rrf_k: int = 60,
    ) -> None:
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        self._lexical = lexical
        self._vector = vector
        self._rrf_k = rrf_k

    @property
    def vector_enabled(self) -> bool:
        """当前是否启用向量路（False = 降级为纯词法）。"""
        return self._vector is not None

    def upsert(self, chunks: Iterable[KnowledgeChunk]) -> None:
        """写入分块：两路同步写入（向量路存在时），同 chunk_id 覆盖。"""
        chunk_list = list(chunks)
        self._lexical.upsert(chunk_list)
        if self._vector is not None:
            self._vector.upsert(chunk_list)

    def delete_document(self, document_id: str) -> None:
        """删除某文档的全部分块：两路同步删除（与 add_documents 的
        整文档替换语义配套）。"""
        self._lexical.delete_document(document_id)
        if self._vector is not None:
            self._vector.delete_document(document_id)

    def chunk(self, chunk_id: str) -> KnowledgeChunk | None:
        """按 chunk_id 取回分块（I2）：转发给词法路（永不降级的底线）。

        两路同源（同一批 upsert 写入），以词法路为准——与 RRF 融合
        时「chunk/citation 以词法路对象为准」同一约定（见 search）。
        """
        return self._lexical.chunk(chunk_id)

    def chunks_of_document(self, document_id: str) -> list[KnowledgeChunk]:
        """读取某文档的全部分块（I2 浏览）：转发给词法路。

        与 chunk 同一约定（两路同源、以词法路为准，见 chunk 注释）。
        词法路是 InMemory / Sqlite 实现，两者都有 chunks_of_document；
        getattr 兜底与 search 降级语义一致——缺该能力（理论上的替身
        索引）返回空列表，不抛错（browse 端点经服务层同样兜底）。
        """
        getter = getattr(self._lexical, "chunks_of_document", None)
        if getter is None:
            return []
        return list(getter(document_id))

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """混合检索：两路各自过滤打分 → RRF 融合 → 截断 top_k。

        降级路径（vector 为 None）：完全透传词法路，分数与排序和
        词法单路一致（不抛错）。过滤语义见模块注释第 4 节。
        """
        if not query.strip() or top_k <= 0:
            return []
        if self._vector is None:
            # 降级：词法单路即最终结果（见模块注释第 5 节）。
            return self._lexical.search(
                query, top_k, metadata_filter=metadata_filter
            )
        # 候选窗口：两路各自多取一些再融合（见模块注释第 3 节）。
        candidate_top_k = max(top_k * 2, 10)
        lexical_hits = self._lexical.search(
            query, candidate_top_k, metadata_filter=metadata_filter
        )
        vector_hits = self._vector.search(
            query, candidate_top_k, metadata_filter=metadata_filter
        )
        # RRF 融合：chunk_id → 融合分 = Σ 1/(k + 该路排名)（公式见模块注释）。
        fused: dict[str, float] = {}
        for hits in (lexical_hits, vector_hits):
            for rank, hit in enumerate(hits, start=1):  # 排名从 1 起：第 1 名权重 1/(k+1) 最高
                fused[hit.chunk.chunk_id] = (
                    fused.get(hit.chunk.chunk_id, 0.0) + 1.0 / (self._rrf_k + rank)
                )
        # chunk/citation 完整对象取自两路命中；两路数据同源（同一批
        # upsert 写入），内容一致，但以词法路对象为准——词法路是永不
        # 降级的底线索引，因此拼接顺序是向量在前、词法在后（dict 推导
        # 后者覆盖前者，词法路优先）。
        by_chunk_id = {
            hit.chunk.chunk_id: hit for hit in [*vector_hits, *lexical_hits]
        }
        # 排序：融合分降序，同分按 chunk_id 升序（与两路平局规则一致）。
        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        return [
            SearchHit(
                chunk=by_chunk_id[chunk_id].chunk,
                citation=by_chunk_id[chunk_id].citation,
                score=score,
            )
            for chunk_id, score in ordered[:top_k]
        ]

    def close(self) -> None:
        """关闭底层索引（转发给两路；内存索引没有 close 方法则跳过）。"""
        for index in (self._lexical, self._vector):
            if index is None:
                continue
            closer = getattr(index, "close", None)
            if closer is not None:
                closer()


def open_vector_index_if_available(
    db_path: str | Path,
    provider: EmbeddingProvider | None = None,
) -> SqliteVectorKnowledgeIndex | None:
    """按「可用才开」原则打开向量库；不可用返回 None（自动降级，不抛错）。

    触发条件与降级语义（面向初学者）：
    1. 文件不存在（最常见：先前入库未带 --vector，向量库从未创建）
       → 返回 None，检索降级为纯词法；
    2. 文件存在但打不开（换过 embedding provider 导致维度不匹配、
       BLOB 损坏、SQLite 文件损坏等）→ 返回 None，同样降级——向量
       路只是可选增强，任何原因不可用都不应阻断检索（可用性优先）；
    3. 打开成功 → 返回索引实例，调用方把它传给 HybridKnowledgeIndex
       启用混合检索。
    注意：本函数不会创建数据库文件（文件不存在直接返回 None，避免
    只读场景——如 ingest 脚本 --verify——白白生成空库文件）。
    """
    if not Path(db_path).exists():
        return None
    try:
        return SqliteVectorKnowledgeIndex(
            db_path, provider=provider or HashEmbeddingProvider()
        )
    except (ValueError, sqlite3.Error, OSError):
        # 打开失败 = 向量路不可用 → 降级为纯词法（不抛错）：
        # - ValueError：向量库维度与当前 embedding provider 不一致
        #   （换过 provider 未重建库）、向量 BLOB 损坏、metadata JSON
        #   损坏——都是 vector_index 加载期明确抛出的数据问题；
        # - sqlite3.Error：库文件本身损坏或不可访问（SQLite 把权限/
        #   路径问题包装成 OperationalError，也属本类）；
        # - OSError：文件系统层面打不开（如权限拒绝的极端情况）。
        # 只捕获这三类「数据 / 环境问题」：编程错误（AttributeError、
        # TypeError 等）不在此列——它们说明代码有 bug，应暴露出来而
        # 不是被静默吞掉（盲目 except Exception 会掩盖这类错误）。
        return None


__all__ = ["HybridKnowledgeIndex", "open_vector_index_if_available"]
