"""Application service that connects document chunking with an index."""

from __future__ import annotations

from collections.abc import Iterable

from .chunking import chunk_documents, chunk_documents_semantic
from .index import KnowledgeIndex
from .models import KnowledgeChunk, KnowledgeDocument, SearchHit
from .policy import RetrievalPolicy
from .retrieval import (
    AdaptiveSearchResult,
    QueryRefiner,
    QueryRewriter,
    Reranker,
    adaptive_search,
    multi_query_search,
)

# 可选分块策略（S3-T2）：
# - "character"：字符窗口分块（默认，S3-T1 起的行为，保持不变）；
# - "semantic"：按章节标题 / 段落边界分块，并保护公式与代码块不被截断。
_CHUNKING_STRATEGIES = frozenset({"character", "semantic"})


class KnowledgeService:
    """Provide the small write, delete, and search API used by agent tools."""

    def __init__(
        self,
        index: KnowledgeIndex,
        *,
        chunk_size: int = 1000,
        overlap: int = 100,
        chunking: str = "character",
        max_chunk_size: int = 2000,
        min_chunk_size: int = 200,
        rewriter: QueryRewriter | None = None,
        reranker: Reranker | None = None,
        policy: RetrievalPolicy | None = None,
        refiner: QueryRefiner | None = None,
        relevance_threshold: float | None = None,
        max_refine_rounds: int = 2,
    ) -> None:
        """初始化服务。

        参数说明（面向初学者）：
        - chunking：分块策略，"character"（默认）或 "semantic"。
          "character" 走 S3-T1 的字符窗口分块，行为完全不变；
          "semantic" 按章节标题/段落边界切分（见
          chunking.chunk_document_semantic 的规则说明）。
        - chunk_size / overlap：仅 character 策略使用（窗口大小与重叠）；
        - max_chunk_size / min_chunk_size：仅 semantic 策略使用
          （目标块大小上限；超长段落切分时「最近行边界」取舍的最小值）。
        - rewriter（S4-T1）：查询改写器，可选。None 表示默认
          IdentityQueryRewriter——不改写，检索行为与 S3 完全一致
          （零回归）；传入自定义改写器后，每次 search 会把 query
          改写为多个变体、每变体各检索一次、按 chunk_id 去重后以
          max 分数合并排序（协议与语义详见 retrieval.py 模块注释；
          改写失败自动降级为原始 query 单路，不抛错）。
        - reranker（S4-T2）：重排器，可选。None 表示默认
          IdentityReranker——不重排，行为与 S4-T1 完全一致（零回归）；
          传入自定义重排器后，每次 search 会先做初检（单路或多路
          合并）、截出候选窗口 max(top_k×2, 10)，再把候选交给重排器
          重新排序，最后按重排后的顺序截断 top_k（协议与语义详见
          retrieval.py 模块注释第 7 节；重排失败自动保持初检结果，
          不抛错）。
        - policy（S4-T3）：检索必要性策略，可选，仅 adaptive_search
          使用。None 表示默认 AlwaysRetrievalPolicy——总是检索，行为
          与 S4-T2 完全一致（零回归）；传入 HeuristicRetrievalPolicy
          等实现后，寒暄、纯计算等简单问题判定为「不需要检索」，
          直接返回空结果 + 元数据（规则与理由详见 policy.py 模块
          注释；判定失败自动降级为需要检索，不抛错）。
        - refiner（S4-T3）：查询精化器，可选，仅 adaptive_search
          使用。None 表示不重检——阈值未达标时单轮停止（零回归）；
          传入精化器后，未达标会 refine 出新查询重检（上限见
          max_refine_rounds；精化失败自动停止重检，不抛错，语义详见
          retrieval.py 模块注释第 8 节）。
        - relevance_threshold（S4-T3）：相关性阈值，可选，仅
          adaptive_search 使用。None 表示不启用阈值判定（默认，零
          回归——不注入阈值时行为与 S4-T2 完全一致）；启用时必须
          > 0，且按当前索引的 SearchHit.score 量纲取值（词法 = 命中
          词数、RRF = 融合分、余弦 = 相似度，量纲问题与建议口径详见
          retrieval.py 模块注释第 8 节第 2 点）。
        - max_refine_rounds（S4-T3）：重检次数上限（默认 2，可配置；
          须 ≥ 0 且 ≤ 10），仅 refiner 非 None 时生效。上限 10 是
          成本软上限：每轮重检 = 一次完整检索（未来 LLM 精化器还有
          模型调用），上限过高会让单次查询成本失控。
        """
        if chunking not in _CHUNKING_STRATEGIES:
            raise ValueError("chunking must be 'character' or 'semantic'")
        if relevance_threshold is not None and relevance_threshold <= 0:
            raise ValueError("relevance_threshold must be positive when enabled")
        if max_refine_rounds < 0:
            raise ValueError("max_refine_rounds must be >= 0")
        # 成本软上限：与 adaptive_search 的校验一致（每轮重检 = 一次
        # 完整检索，上限过高成本失控）。
        if max_refine_rounds > 10:
            raise ValueError("max_refine_rounds must be <= 10")
        self._index = index
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._chunking = chunking
        self._max_chunk_size = max_chunk_size
        self._min_chunk_size = min_chunk_size
        self._rewriter = rewriter
        self._reranker = reranker
        self._policy = policy
        self._refiner = refiner
        self._relevance_threshold = relevance_threshold
        self._max_refine_rounds = max_refine_rounds

    def add_documents(self, documents: Iterable[KnowledgeDocument]) -> list[KnowledgeChunk]:
        """Replace the supplied documents, then return their stored chunks."""
        document_batch = list(documents)
        coordinates: set[tuple[str, int | None]] = set()
        for document in document_batch:
            coordinate = (document.document_id, document.page)
            if coordinate in coordinates:
                raise ValueError("duplicate document page in one batch")
            coordinates.add(coordinate)

        # 按构造时选定的分块策略分块：character 保持 S3-T1 行为不变；
        # semantic 按标题/段落边界切分（公式/代码保护、坐标语义见
        # chunking 模块注释）。两种策略产出的 chunk 坐标都可回溯原文，
        # 后续的索引写入与检索链路完全复用。
        if self._chunking == "semantic":
            chunks = chunk_documents_semantic(
                document_batch,
                max_chunk_size=self._max_chunk_size,
                min_chunk_size=self._min_chunk_size,
            )
        else:
            chunks = chunk_documents(
                document_batch,
                chunk_size=self._chunk_size,
                overlap=self._overlap,
            )
        # 同一 PDF 的多页共用 document_id，因此先统一清理，再写入整批分块。
        document_ids = dict.fromkeys(
            document.document_id for document in document_batch
        )
        for document_id in document_ids:
            self._index.delete_document(document_id)
        self._index.upsert(chunks)
        return chunks

    def delete_document(self, document_id: str) -> None:
        """Remove all chunks for a document."""
        self._index.delete_document(document_id)

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        """Validate public search inputs, then delegate ranking to the index.

        S4-T1 多路检索编排（面向初学者）：search 现在走 retrieval.py
        的 multi_query_search——默认不改写（Identity 零回归，结果与
        S3 逐项一致）；注入改写器后，query 会被改写为多个变体、每个
        变体各检索一次、按 chunk_id 去重后以 max 分数合并排序；改写
        失败自动降级为原始 query 单路检索，不抛错（语义详见
        retrieval.py 模块注释第 3/4/5 节）。

        S4-T2 重排序（面向初学者）：search 的流程是「初检 → 重排 →
        截断」。默认不重排（IdentityReranker 零回归，结果与 S4-T1
        逐项一致）；注入重排器后，初检合并结果先截出候选窗口
        （max(top_k×2, 10) 名）交给重排器重新排序，再按重排后的顺序
        截断最终 top_k；重排失败自动保持初检结果，不抛错（语义详见
        retrieval.py 模块注释第 7 节）。

        过滤语义（S3-T3，面向初学者）：metadata_filter 是「键 → 值」
        字典，例如 {"source": "ml-zhouzhihua", "difficulty": "intermediate"}
        表示「只在这本书、这个难度里检索」。规则：
        - 多键之间是「并且」关系，全部满足才入选；
        - 键 "source" 限定逻辑来源（某本书）；其余键匹配 chunk 的
          领域字段 subject/difficulty/chapter/section/tags（字段约定
          见 models.py 模块注释）；
        - 过滤在打分排序之前生效（索引层实现），top_k 截断发生在
          过滤之后——过滤后不足 top_k 个就返回全部匹配；
        - 没有任何匹配时返回空列表（不报错）；
        - 多路检索下过滤条件透传给每一个变体，被过滤的 chunk 不会
          进入任何变体、自然也不进合并结果（与单路语义一致）。
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k must be between 1 and 10")
        return multi_query_search(
            self._index,
            query,
            top_k,
            rewriter=self._rewriter,
            reranker=self._reranker,
            metadata_filter=metadata_filter,
        )

    def adaptive_search(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: dict[str, str] | None = None,
        policy: RetrievalPolicy | None = None,
        relevance_threshold: float | None = None,
        refiner: QueryRefiner | None = None,
    ) -> AdaptiveSearchResult:
        """自适应检索（S4-T3）：必要性判断 → 检索 → 阈值判定 → 多轮重检。

        （面向初学者）search 是「固定单轮检索」；本方法是 S4-T3 的
        自适应编排入口，流程与语义详见 retrieval.py 的 adaptive_search
        与模块注释第 8 节，构造时注入的 policy / refiner /
        relevance_threshold / max_refine_rounds 在此生效：

        1. 必要性判断（policy，默认 AlwaysRetrievalPolicy——总是检索，
           零回归）：寒暄、纯计算等简单问题判定为「不需要检索」→
           返回空结果 + 元数据（needed=False，reason 说明为什么），
           上层直接作答；
        2. 检索：与 search 完全相同的 multi_query_search 链路
           （rewriter / reranker / metadata_filter 原样透传）；
        3. 阈值判定（relevance_threshold，默认 None 不启用）：本轮
           最高分 < 阈值 → 未达标。未达标时结果照常返回，但元数据
           threshold_met=False——上层不应把结果当证据注入，应向
           用户说明「知识库未覆盖」而非强行作答；
        4. 多轮重检（refiner + max_refine_rounds，默认不重检）：未
           达标 → refine 出新查询 → 重检 → 再判定，达到上限仍未达标
           → 停止。每轮检索与精化历史记录在返回的 RetrievalMetadata
           （rounds / refine_history / stopped_reason）里，由上层
           （工具/图）转成事件——检索层不依赖 core/events.py。

        S4-T3 工具层注入（本任务的扩展）：policy / relevance_threshold
        / refiner 三个参数允许调用方（如 create_search_knowledge_tool）
        按次覆盖构造时配置——None 表示沿用构造时配置（默认，零回归），
        非 None 表示本次调用改用注入值。为什么需要覆盖：工具装配方
        （api 层）构造 service 时可能没有检索配置，工具自身却允许
        装配方注入自适应配置（见 tools.py 注释）；没有这个覆盖口，
        工具注入的配置就无法生效。校验与降级语义与构造时配置一致
        （relevance_threshold > 0、上限校验在 retrieval.adaptive_search
        内部兜底）。
        边界（M-2）：None 恒表示「沿用构造时配置」，本方法不提供
        「覆盖为空（显式关闭）」的通道——如需在工具层关闭构造时
        已配置的 relevance_threshold / refiner（如临时退化为单轮
        检索），请修改 service 构造配置或另建 service 实例，避免
        None 语义歧义。

        返回：AdaptiveSearchResult（hits + RetrievalMetadata），hits
        的形态与 search 一致（分数、排序、citation 语义不变）。所有
        失败路径（policy / refiner 异常）都不抛错（降级语义详见
        retrieval.py 的 _safe_policy / _safe_refine）。
        """
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k must be between 1 and 10")
        return adaptive_search(
            self._index,
            query,
            top_k,
            # 覆盖语义：非 None 用调用方注入值，None 沿用构造时配置。
            policy=policy if policy is not None else self._policy,
            rewriter=self._rewriter,
            reranker=self._reranker,
            refiner=refiner if refiner is not None else self._refiner,
            relevance_threshold=(
                relevance_threshold
                if relevance_threshold is not None
                else self._relevance_threshold
            ),
            max_refine_rounds=self._max_refine_rounds,
            metadata_filter=metadata_filter,
        )


__all__ = ["KnowledgeService"]
