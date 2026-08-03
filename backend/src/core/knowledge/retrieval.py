"""S4-T1/S4-T2 检索编排层：多路检索（Query 改写与多变体联合）与重排序。

（面向初学者的设计说明，按功能模块）

1. 本模块的位置：查询编排层
   index.py / vector_index.py / hybrid.py 是「索引实现层」：给定一个
   query，返回一个排序好的结果列表。本模块是索引之上的「查询编排
   层」：把用户的一个问题改写成多个检索变体，每个变体各检索一次，
   再把结果按 chunk_id 去重合并成一个答案列表。
   分层理由（两条职责边界）：
   - 索引层不关心「查询是怎么来的」——它只负责给定 query 怎么打分
     排序；改写与合并逻辑放进索引层会让索引承担无关职责；
   - 编排层不重新实现打分——它只负责「把哪些 query 送进索引、拿到
     结果后怎么合并」，分数语义完全来自索引层（词法命中数 / RRF
     融合分 / 余弦相似度，取决于挂的是哪个索引）。
   后续 S4-T3 自适应策略也在检索服务层扩展，独立模块给它们留好
   扩展点（service.py 保持薄，只做输入校验与委托）。S4-T2 重排序
   是本模块的第 7 节：初检 → 重排 → 截断。

2. 改写器协议（QueryRewriter）
   QueryRewriter 是「把一个查询改写成一个或多个检索变体」的协议：
       rewrite(query) -> list[str]
   - 返回的每个字符串都是一个检索变体，会被各自送入索引检索一次；
   - 协议用 typing.Protocol 声明，实现方只要提供 rewrite 方法即可
     （鸭子类型），因此改写器可注入、可替换：测试替身、规则改写、
     未来上层的 LLM 改写器都实现同一个协议；
   - 默认实现 IdentityQueryRewriter 原样返回 [query]——不改写。

3. 为什么默认不改写（零回归）
   IdentityQueryRewriter 是默认值：不传改写器时，编排退化为「单变体
   检索」，即等价于直接调用索引 search。因此 S3 以来的行为（分数、
   排序、过滤）逐项不变，既有测试零回归。改写的收益（同义改写提升
   召回）是可选增强，由调用方显式注入改写器后才启用。

4. 联合检索与合并排序：max 分数合并
   每个变体各检索一次（词法/向量/混合取决于 service 挂的索引，编排
   层不感知），然后按 chunk_id 去重合并。合并分选择「取该 chunk 在
   所有变体下的最高分」（max 合并），排序为分数降序、同分按
   chunk_id 升序（与索引层平局规则一致，保证结果确定、可复现）。
   为什么是 max 而不是其他方案：
   - 为什么不用 RRF：hybrid.py 用 RRF 是因为词法与向量两路「量纲
     不同」（命中词数 vs 余弦相似度）不能直接比较；而多变体的每一
     路都走同一个索引、同一个打分函数，分数天然同量纲、可直接
     比较，不需要 RRF 这种「只看排名」的间接融合；
   - 为什么不用加权：给每个变体分配权重（0.5/0.5？改写置信度？）
     没有先验依据，调参敏感且难解释（与 hybrid.py 第 2 节同一论证）；
   - 为什么不用「保留首个命中」：变体顺序会影响结果、丢失分数
     信息（后出现的变体即使分数更高也被忽略），排序质量差；
   - max 的语义直白：chunk 的最终分 = 它在任何变体下的最好成绩，
     「任何一个变体强烈命中都算数」——改写变体存在的意义正是
     「换个说法再查一遍」，某变体命中即代表该说法与 chunk 相关。
   候选窗口：每个变体先取 max(top_k×2, 10) 名再合并，合并后截断
   top_k。原因与 hybrid.py 第 3 节相同：若每变体只取 top_k 名，一个
   「在某变体下排名第 top_k+1 但分数很高」的 chunk 会在合并前被
   丢掉；多取几名的成本可忽略（本项目本来就是全表打分）。

5. 降级语义（改写失败不阻断检索，可用性优先）
   - 改写器抛异常（未来 LLM 改写器的网络/模型错误都属于此类）：
     捕获并记录 warning，降级为原始 query 单路检索——结果与未注入
     改写器时完全一致，不抛错；
   - 改写器返回空列表 / 全是空白 / 全被去重：同样降级为原始 query；
   - 返回类型不合法（不是 list[str]，如返回 None 或含非字符串元素）：
     与抛异常同等对待——改写器是外部组件，任何「改写不可用」都不应
     阻断检索（可用性优先）；
   - 变体中的空白字符串会被跳过，重复变体会被去重（dict.fromkeys
     保序），避免无效或重复的检索；
   - 为什么捕获 Exception 而不是更窄的类型：改写器是注入的外部
     组件，任何异常都意味着「改写不可用」，检索不应被可选的增强
     阻断（与 open_vector_index_if_available 的「可用才开」哲学一致）；
     不捕获 BaseException，KeyboardInterrupt/SystemExit 照常穿透。
   降级如何被看见：本模块用 Python 标准库 logging 记录 warning
   （core 内目前没有日志基础设施，这里是最轻量的做法），默认会在
   stderr 输出，便于教学调试时看到「改写失败但检索照常」；降级
   不影响任何返回值，上层无需感知。

6. LLM 改写器扩展点（为什么本模块不实现真实 LLM 调用）
   本模块只提供协议 + 替身，刻意不实现真实 LLM 改写器，取舍如下：
   - core 的模型调用位于 nodes/react_agent.py 的 ChatModel 协议
     （Agent 层）。检索层若直接调模型，会让 knowledge 包反向依赖
     Agent 层（或被迫接收 ChatModel 注入），把「改写质量」与
     「检索正确性」两类关注点耦合在一起，测试也必须 mock 模型；
   - 真实 LLM 改写器应由上层（Agent / 工具层）注入：上层已经持有
     ChatModel，可实现一个 QueryRewriter，在 rewrite() 里调用模型
     生成多个检索变体。实现要点：限制变体数量上限（防成本失控）、
     解析失败或模型异常时抛错或返回 [query]（本模块的降级语义会
     兜底）、控制延迟。
   扩展点已就绪：任何「rewrite(query) -> list[str]」的实现都能被
   KnowledgeService 接受，无需改动本模块。

7. 重排序（S4-T2）：初检 → 重排 → 截断
   重排是检索流程的最后一个可选步骤，位于「初检」与「最终截断」
   之间，完整流程为：
     改写 → 每变体初检 → max 合并去重 → 截出候选窗口（Top-N）
     → 重排器重排 → 截断最终 Top-K
   （1）重排器协议（Reranker）
       rerank(query, hits, top_k) -> list[SearchHit]
       - query 是原始用户问题（与改写器一致，拿到的是用户问题而
         不是检索变体）；
       - hits 是初检合并后的候选列表；
       - top_k 是最终要返回的结果数——真实重排器（如 Cross-Encoder
         批量打分）可据此控制打分成本；
       - 返回列表的顺序即最终顺序：可以只改顺序（本模块测试替身
         就是这样），也可以顺手更新 score（真实重排器会这么做），
         协议不强制修改 score，只约定顺序。
   （2）为什么重排前要留候选窗口（N ≥ K，N = max(top_k×2, 10)）
       初检分数来自词法命中数 / RRF / 余弦相似度，衡量的是「检索
       层面的相似」，与「用户问题真正想要什么」有差距——重排的
       意义正是用更强的模型把「检索排在前但实际不相关」的项压
       下去、把「检索排名靠后但确实相关」的项提上来。若初检只留
       top_k 名，被压下去的项根本没有机会被重排器看到，重排就
       失去了意义。候选窗口取 max(top_k×2, 10)：与每变体初检的
       窗口同一数值（见第 4 节），成本可忽略（本项目本来就是全表
       打分）。
   （3）Identity 默认零回归
       默认不注入重排器（或显式注入 IdentityReranker）时，候选
       顺序不变，截断逻辑与 S4-T1 完全一致：候选窗口 ≥ top_k，
       「先截候选窗口再截 top_k」与「直接截 top_k」结果相同，
       既有行为逐项不变（分数、顺序、过滤、降级）。
   （4）替身重排器（测试用）
       测试里实现一个确定性替身（按 query 与 content 的词重合度
       重新打分排序），用来证明「重排生效」：构造「初检首位不是
       正确答案」的场景，断言重排后首位变为期望项。替身只演示
       协议与流程位置，不模拟真实模型的打分质量。
   （5）降级语义（重排失败不阻断检索，可用性优先）
       - rerank() 抛异常（未来 Cross-Encoder 的模型/网络错误都属
         于此类）、返回类型不合法（不是 list[SearchHit]，如返回
         None 或含非 SearchHit 元素）→ 保持初检候选顺序，记录
         warning，不抛错——与改写降级（第 5 节）同一哲学：重排
         是可选的增强，任何「重排不可用」都不应阻断检索。
       - 注意与改写器降级刻意不对称：改写器返回空 / 全空白会降级
         为原始 query 单路（「没有可用变体」通常意味着改写器异常，
         见第 5 节）；重排器返回空列表则不是降级触发点——空返回
         是重排器的合法裁决（它明确认为候选均不相关），直接透传
         空结果，不记录 warning。若把空返回也降级为初检候选，
         反而违背了「重排结果说了算」的意图。
   （6）Cross-Encoder / LLM 重排器扩展点（为什么本模块不实现真实
       模型）
       与 LLM 改写器同理（第 6 节）：core 的模型调用位于
       nodes/react_agent.py 的 ChatModel 协议（Agent 层），检索层
       不反向依赖 Agent 层、不接收模型注入，避免把「重排质量」
       与「检索正确性」两类关注点耦合。真实重排器应由上层实现
       Reranker 协议后注入：Cross-Encoder 可在 rerank() 里对候选
       批量打分并更新 score；LLM 重排可在 rerank() 里让模型挑出
       最相关的前几个。实现要点：候选规模已由协议传入（top_k 可
       控制精细打分范围）、失败时抛错（本模块降级兜底）或直接
       返回原列表。扩展点已就绪：任何
       「rerank(query, hits, top_k) -> list[SearchHit]」的实现都能
       被 KnowledgeService 接受，无需改动本模块。
"""

from __future__ import annotations

import logging
from typing import Protocol

from .index import KnowledgeIndex
from .models import SearchHit

logger = logging.getLogger(__name__)


class QueryRewriter(Protocol):
    """查询改写器协议：把一个查询改写成多个检索变体（可注入、可替换）。

    实现方只需提供 rewrite 方法（鸭子类型，见模块注释第 2 节）。
    返回列表中的每个字符串是一个检索变体；返回 [query] 表示不改写
    （见 IdentityQueryRewriter）。
    """

    def rewrite(self, query: str) -> list[str]:
        """改写查询，返回一个或多个检索变体。"""
        ...


class IdentityQueryRewriter:
    """默认改写器：原样返回 [query]，即不改写（零回归，见模块注释第 3 节）。

    为什么需要它：让「不启用改写」也走改写器协议，调用方不需要
    判空逻辑；行为与直接调用索引 search 完全一致。
    """

    def rewrite(self, query: str) -> list[str]:
        return [query]


class Reranker(Protocol):
    """重排器协议：对初检候选重新排序（可注入、可替换，见模块注释第 7 节）。

    实现方只需提供 rerank 方法（鸭子类型）。三个参数的含义：
    - query：原始用户问题（不是检索变体）；
    - hits：初检合并后的候选列表（长度 ≤ max(top_k×2, 10)，即
      「初检 Top-N 候选窗口」）；
    - top_k：最终要返回的结果数（真实重排器可据此控制精细打分
      的成本）。
    返回的列表顺序即最终顺序：可以只改顺序，也可以顺手更新 score
    （真实 Cross-Encoder 会这么做）；协议不强制修改 score。返回应
    是 hits 的一个重排：可以丢弃部分项（重排器裁决不相关的项），
    但不应增补 hits 之外的项（重排不负责扩展召回）。
    """

    def rerank(
        self, query: str, hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        """重排候选，返回按新顺序排列的 SearchHit 列表。"""
        ...


class IdentityReranker:
    """默认重排器：原样返回候选，即不重排（零回归，见模块注释第 7 节第 3 点）。

    为什么需要它：与 IdentityQueryRewriter 同理——让「不启用重排」
    也走重排器协议，调用方不需要判空；行为与 S4-T1 及更早完全一致。
    """

    def rerank(
        self, query: str, hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        return hits


def multi_query_search(
    index: KnowledgeIndex,
    query: str,
    top_k: int,
    *,
    rewriter: QueryRewriter | None = None,
    reranker: Reranker | None = None,
    metadata_filter: dict[str, str] | None = None,
) -> list[SearchHit]:
    """多变体联合检索 + 可选重排：改写 → 初检合并 → 重排 → 截断。

    参数（面向初学者）：
    - index：任意实现 KnowledgeIndex 协议的索引。词法单路、向量单路、
      HybridKnowledgeIndex 混合都行——每个变体都走它的 search，编排
      层不感知索引内部是单路还是混合；
    - rewriter：改写器；None 等价于 IdentityQueryRewriter（零回归，
      见模块注释第 3 节）；
    - reranker（S4-T2）：重排器；None 等价于 IdentityReranker——不重排，
      候选按初检顺序直接截断 top_k（零回归，见模块注释第 7 节）；
      注入后，初检合并结果先截出候选窗口（max(top_k×2, 10) 名）交给
      重排器，再按重排后的顺序截断最终 top_k；
    - metadata_filter：S3-T3 过滤条件，透传给每一个变体的检索（先
      过滤后排序的语义由索引层保证，与单路检索完全一致）。
    返回：合并去重后按分数降序（同分按 chunk_id 升序）的前 top_k 个
    SearchHit（注入重排器时按重排后的顺序）；改写失败自动降级为原始
    query 单路检索（见模块注释第 5 节）、重排失败保持初检候选（见模块
    注释第 7 节第 5 点），均不抛错。重排器返回空列表是其合法裁决，
    直接返回空结果（见模块注释第 7 节第 5 点）。
    """
    active = rewriter if rewriter is not None else IdentityQueryRewriter()
    variants = _safe_variants(active, query)
    # 候选窗口：每个变体多取一些再合并（理由见模块注释第 4 节）。
    # 合并后的前 candidate_top_k 名构成「初检 Top-N 候选窗口」——这个
    # 窗口（而非直接 top_k）交给重排器，保证重排有足够的候选可挑
    # （模块注释第 7 节第 2 点）。
    candidate_top_k = max(top_k * 2, 10)
    # max 合并：chunk_id → (最高分, 该分的 SearchHit)。
    # 同一 chunk 在不同变体下多次出现 → dict 按 chunk_id 天然去重；
    # 分数取 max——所有变体走同一打分函数、量纲一致，可直接比较。
    best: dict[str, tuple[float, SearchHit]] = {}
    for variant in variants:
        for hit in index.search(
            variant, candidate_top_k, metadata_filter=metadata_filter
        ):
            previous = best.get(hit.chunk.chunk_id)
            if previous is None or hit.score > previous[0]:
                best[hit.chunk.chunk_id] = (hit.score, hit)
    # 排序：分数降序，同分按 chunk_id 升序（与索引层平局规则一致，
    # 结果确定、可复现）。
    ordered = sorted(best.items(), key=lambda item: (-item[1][0], item[0]))
    # 初检 → 重排 → 截断（S4-T2，模块注释第 7 节）：先截出候选窗口，
    # 重排器对候选重新排序（未注入或 Identity 时顺序不变，零回归），
    # 最后才截断最终 top_k——截断发生在重排之后。
    candidates = [entry[1] for _, entry in ordered[:candidate_top_k]]
    if reranker is not None:
        candidates = _safe_rerank(reranker, query, candidates, top_k)
    return candidates[:top_k]


def _safe_variants(rewriter: QueryRewriter, query: str) -> list[str]:
    """执行改写并兜底：任何失败都降级为原始 query 单路（模块注释第 5 节）。

    返回的列表保证：非空、无空白项、无重复项（保序）。
    降级触发点（全部不抛错）：
    - rewrite() 抛异常（LLM 网络/解析错误等外部故障）；
    - 返回类型不合法：不是 list[str]（协议约定，但改写器是外部组件，
      可能返回 None 或含非字符串元素）——一律视为「改写不可用」；
    - 清洗后为空（空列表 / 全空白 / 全被去重）。
    """
    # 空白 query 不是改写失败：直接原样返回，不打降级 warning（避免
    # 误报「改写结果为空」；索引层对空白 query 本来就返回空结果）。
    if not query.strip():
        return [query]
    try:
        raw = rewriter.rewrite(query)
        # 协议要求返回 list[str]，但外部组件可能违反（返回 None / 含
        # 非字符串元素）。这类返回值与抛异常同等对待：都是「改写
        # 不可用」，降级为原始 query，不抛错。
        if not isinstance(raw, list) or not all(
            isinstance(variant, str) for variant in raw
        ):
            raise TypeError("query rewriter must return a list of strings")
        # 跳过空白变体；dict.fromkeys 去重并保留首次出现的顺序，
        # 避免对同一变体重复检索。
        variants = list(
            dict.fromkeys(variant.strip() for variant in raw if variant.strip())
        )
    except Exception as exc:  # noqa: BLE001 — 外部组件失败 → 降级是设计意图
        # （改写器是注入的外部组件，可能抛任意异常：LLM 网络错误、解析错误
        # 等；任何失败都意味着改写不可用，检索不应被可选增强阻断，故收窄
        # 异常类型不现实，见模块注释第 5 节；不捕获 BaseException。）
        logger.warning(
            "查询改写失败（%s），降级为原始 query 单路检索", type(exc).__name__
        )
        return [query]
    if not variants:
        logger.warning("查询改写结果为空，降级为原始 query 单路检索")
        return [query]
    return variants


def _safe_rerank(
    reranker: Reranker,
    query: str,
    candidates: list[SearchHit],
    top_k: int,
) -> list[SearchHit]:
    """执行重排并兜底：任何失败都保持初检候选（模块注释第 7 节第 5 点）。

    返回的列表保证：顺序即最终顺序。空返回是合法结果——重排器
    明确裁决「候选均不相关」时返回空列表，直接透传（返回空结果，
    不降级、不记录 warning），与改写器的「空结果 → 降级」刻意
    不对称（理由见模块注释第 7 节第 5 点）。
    降级触发点（全部不抛错，保持初检候选）：
    - rerank() 抛异常（未来 Cross-Encoder 的模型/网络错误都属于此类）；
    - 返回类型不合法：不是 list[SearchHit]（协议约定，但重排器是外部
      组件，可能返回 None 或含非 SearchHit 元素）——一律视为
      「重排不可用」，保持初检候选顺序。
    为什么捕获 Exception 而不是更窄的类型：与 _safe_variants 同一
    论证（见模块注释第 5 节）——重排器是注入的外部组件，任何异常都
    意味着重排不可用，检索不应被可选的增强阻断；不捕获 BaseException。
    """
    try:
        reranked = reranker.rerank(query, candidates, top_k)
        # 协议要求返回 list[SearchHit]，但外部组件可能违反（返回 None /
        # 含非 SearchHit 元素）。这类返回值与抛异常同等对待：都是
        # 「重排不可用」，保持初检候选，不抛错。
        if not isinstance(reranked, list) or not all(
            isinstance(hit, SearchHit) for hit in reranked
        ):
            raise TypeError("reranker must return a list of SearchHit")
        return reranked
    except Exception as exc:  # noqa: BLE001 — 外部组件失败 → 降级是设计意图
        # （重排器是注入的外部组件，可能抛任意异常：模型网络错误、解析
        # 错误等；任何失败都意味着重排不可用，检索不应被可选增强阻断，
        # 故收窄异常类型不现实，见模块注释第 7 节第 5 点；不捕获
        # BaseException。）
        logger.warning(
            "重排失败（%s），保持初检候选顺序", type(exc).__name__
        )
        return candidates


__all__ = [
    "IdentityQueryRewriter",
    "IdentityReranker",
    "QueryRewriter",
    "Reranker",
    "multi_query_search",
]
