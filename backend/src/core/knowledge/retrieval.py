"""S4-T1 多路检索：Query 改写与多变体联合检索（查询编排层）。

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
   后续 S4-T2 重排序、S4-T3 自适应策略也在检索服务层扩展，独立
   模块给它们留好扩展点（service.py 保持薄，只做输入校验与委托）。

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


def multi_query_search(
    index: KnowledgeIndex,
    query: str,
    top_k: int,
    *,
    rewriter: QueryRewriter | None = None,
    metadata_filter: dict[str, str] | None = None,
) -> list[SearchHit]:
    """多变体联合检索：改写 → 每变体各检索一次 → max 合并去重 → 截断。

    参数（面向初学者）：
    - index：任意实现 KnowledgeIndex 协议的索引。词法单路、向量单路、
      HybridKnowledgeIndex 混合都行——每个变体都走它的 search，编排
      层不感知索引内部是单路还是混合；
    - rewriter：改写器；None 等价于 IdentityQueryRewriter（零回归，
      见模块注释第 3 节）；
    - metadata_filter：S3-T3 过滤条件，透传给每一个变体的检索（先
      过滤后排序的语义由索引层保证，与单路检索完全一致）。
    返回：合并去重后按分数降序（同分按 chunk_id 升序）的前 top_k 个
    SearchHit；改写失败时自动降级为原始 query 单路检索（见模块注释
    第 5 节），不抛错。
    """
    active = rewriter if rewriter is not None else IdentityQueryRewriter()
    variants = _safe_variants(active, query)
    # 候选窗口：每个变体多取一些再合并，合并后截断 top_k（理由见
    # 模块注释第 4 节，与 hybrid.py 第 3 节同一思路）。
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
    return [entry[1] for _, entry in ordered[:top_k]]


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


__all__ = ["IdentityQueryRewriter", "QueryRewriter", "multi_query_search"]
