"""S4-T3 自适应检索测试：相关性阈值判定与多轮重检（adaptive_search）。

覆盖清单 A S4-T3 验收标准第 2/3/4 条：
1. 相关性阈值：最高分 ≥ 阈值 → 达标（threshold_met=True）；最高分
   < 阈值 → 未达标（threshold_met=False）——结果照常返回但元数据
   标记「证据不足，不要注入」，top score 与阈值都写入
   RetrievalMetadata（判定逻辑可测试）；
2. 多轮重检：首次不足 → refine → 重检 → 达阈值停止（元数据含
   refine 历史与每轮 top score）；重检次数达到 max_refine_rounds
   上限仍未达标 → 停止（上限生效）；max_refine_rounds=0 → 首轮
   未达标即停止；
3. 降级：refiner 抛异常 / 返回空白 / 返回非 str → 停止重检不抛错
   （记录 warning）；policy 抛异常 / 返回非法值 → 降级为需要检索；
4. 零回归：默认（无策略/阈值/精化器）adaptive_search 与
   service.search 逐项一致（分数、顺序、citation），元数据标记
   单轮、阈值未启用；
5. 校验：阈值 ≤ 0 / max_refine_rounds < 0 / 空 query / top_k 越界
   → ValueError；
6. 组合：rewriter / metadata_filter 透传，阈值判定基于合并后的
   top score。
"""
from __future__ import annotations

import logging

import pytest

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, SearchHit
from core.knowledge.policy import HeuristicRetrievalPolicy, RetrievalDecision
from core.knowledge.retrieval import IdentityQueryRefiner, adaptive_search
from core.knowledge.service import KnowledgeService

# ── 小工具与测试替身 ──────────────────────────────────────────────


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
    source: str | None = None,
) -> KnowledgeChunk:
    """构造一个可直接入库的 chunk（source 用逻辑标识符，非文件路径）。"""
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=source or f"{document_id}.txt",
        page=None,
        start=0,
        end=len(content),
        metadata={},
    )


class _Refiner:
    """精化器测试替身：按映射表精化，记录每次调用 (query, top_score)。

    满足 retrieval.QueryRefiner 协议（鸭子类型）：refine(query,
    top_score) -> str。映射表没有对应 key 时原样返回 query（模拟
    「精化无效」——重检同一 query 结果不变，直到上限停止）。
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, float]] = []

    def refine(self, query: str, top_score: float) -> str:
        self.calls.append((query, top_score))
        return self._mapping.get(query, query)


class _BadRefiner:
    """返回不合法值的精化器替身：模拟外部组件违反协议（返回 None 等）。"""

    def __init__(self, result: object, *, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def refine(self, query: str, top_score: float) -> str:
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


class _BadPolicy:
    """返回不合法值的必要性策略替身：模拟外部组件违反协议（返回 None 等）。"""

    def __init__(self, result: object, *, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def needs_retrieval(self, query: str) -> RetrievalDecision:
        if self._error is not None:
            raise self._error
        return self._result  # type: ignore[return-value]


class _VariantRewriter:
    """改写器测试替身：返回固定变体列表（与 S4-T1 测试同一思路）。"""

    def __init__(self, variants: list[str]) -> None:
        self._variants = variants

    def rewrite(self, query: str) -> list[str]:
        return self._variants


def _ids_and_scores(hits: list[SearchHit]) -> tuple[list[str], list[float]]:
    """把检索结果压成 (chunk_id 列表, score 列表)，便于逐项断言。"""
    return (
        [hit.chunk.chunk_id for hit in hits],
        [hit.score for hit in hits],
    )


# ── 1. 零回归：默认 adaptive_search 与 service.search 一致 ────────


def test_adaptive_default_matches_service_search() -> None:
    """默认参数（无策略/阈值/精化器）：与 service.search 逐项一致。

    词法分数 = 命中英文词数：c1 命中 3 词 → 3.0；c2 不命中。
    断言分数、顺序、citation 逐项一致（S4-T2 行为零回归），且元
    数据标记「单轮、阈值未启用、总是检索」。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c2", "neural network", document_id="ml-b"),
            _chunk("c1", "support vector machine", document_id="ml-a"),
        ]
    )
    service = KnowledgeService(index)

    expected = service.search("support vector machine", top_k=5)
    result = service.adaptive_search("support vector machine", top_k=5)

    assert _ids_and_scores(result.hits) == _ids_and_scores(expected)
    assert [hit.citation.model_dump() for hit in result.hits] == [
        hit.citation.model_dump() for hit in expected
    ]
    meta = result.metadata
    assert meta.needed is True
    assert meta.need_reason == "默认策略：总是检索"
    assert meta.threshold is None
    assert meta.threshold_met is None
    assert len(meta.rounds) == 1
    assert meta.rounds[0].query == "support vector machine"
    assert meta.rounds[0].top_score == 3.0
    assert meta.rounds[0].hit_count == 1
    assert meta.refine_history == ()
    assert meta.stopped_reason == "未启用相关性阈值，单轮检索完成"


# ── 2. 相关性阈值判定 ─────────────────────────────────────────────


def test_threshold_met_returns_hits_with_metadata() -> None:
    """最高分 ≥ 阈值 → 达标：threshold_met=True，元数据记录判定。

    构造（可手算）：c1 "support vector machine" 命中 3 词 → 3.0；
    阈值 2.0 → 3.0 ≥ 2.0 达标。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    service = KnowledgeService(index, relevance_threshold=2.0)

    result = service.adaptive_search("support vector machine", top_k=5)

    assert _ids_and_scores(result.hits) == (["c1"], [3.0])
    meta = result.metadata
    assert meta.threshold == 2.0
    assert meta.threshold_met is True
    assert meta.rounds[0].top_score == 3.0
    assert meta.stopped_reason == "达到相关性阈值"


def test_threshold_not_met_marks_metadata_and_keeps_hits() -> None:
    """最高分 < 阈值 → 未达标：结果照常返回，但元数据标记不注入。

    判定结论（threshold_met=False）供上层决策：Agent 应说明
    「知识库未覆盖」而非把证据强行注入答案——检索层只判定，不做
    决定（模块注释第 8 节第 2 点）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    service = KnowledgeService(index, relevance_threshold=5.0)

    result = service.adaptive_search("support vector machine", top_k=5)

    # 未达标时 hits 照常返回（分数/顺序不变）。
    assert _ids_and_scores(result.hits) == (["c1"], [3.0])
    meta = result.metadata
    assert meta.threshold == 5.0
    assert meta.threshold_met is False
    assert meta.rounds[0].top_score == 3.0  # 3.0 < 5.0 → 未达标
    # 未配置精化器 → 单轮停止，不重检。
    assert meta.stopped_reason == "未配置重检器，未达标即停止"


def test_no_hits_means_top_score_zero_below_threshold() -> None:
    """0 命中 → top score 记 0.0 → 必然未达标（0 < 阈值恒成立）。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    service = KnowledgeService(index, relevance_threshold=1.0)

    result = service.adaptive_search("alpha", top_k=5)

    assert result.hits == []
    assert result.metadata.threshold_met is False
    assert result.metadata.rounds[0].top_score == 0.0
    assert result.metadata.rounds[0].hit_count == 0


# ── 3. 多轮重检：refine → 重检 → 达阈值 / 超上限停止 ─────────────


def test_refine_loop_reaches_threshold() -> None:
    """首次不足 → refine → 重检 → 达阈值停止，元数据含完整过程。

    构造（可手算）：c-gamma "gamma"；query "alpha" 0 命中 → 0.0 <
    0.5 → 精化器把 "alpha" 改成 "gamma" → 重检命中 1 词 → 1.0 ≥
    0.5 达标。断言每轮 (query, top_score, hit_count) 与 refine
    历史精确一致——「重检次数写入事件」由元数据承载（rounds /
    refine_history / stopped_reason）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c-gamma", "gamma", document_id="ml-a")])
    refiner = _Refiner({"alpha": "gamma"})
    service = KnowledgeService(
        index, refiner=refiner, relevance_threshold=0.5
    )

    result = service.adaptive_search("alpha", top_k=5)

    assert _ids_and_scores(result.hits) == (["c-gamma"], [1.0])
    meta = result.metadata
    assert meta.threshold_met is True
    assert meta.stopped_reason == "达到相关性阈值"
    assert [(r.query, r.top_score, r.hit_count) for r in meta.rounds] == [
        ("alpha", 0.0, 0),  # 首轮：0 命中
        ("gamma", 1.0, 1),  # 重检：命中 1 词
    ]
    assert meta.refine_history == ("gamma",)
    # 精化器收到的是上一轮的 (query, top_score)。
    assert refiner.calls == [("alpha", 0.0)]


def test_refine_limit_stops_after_max_rounds() -> None:
    """重检次数达到上限仍未达标 → 停止（上限生效）。

    max_refine_rounds=2：首轮 + 2 次重检共 3 轮；精化器永远返回
    同一 query（0 命中）→ 每轮 top score 都是 0.0 → 第 3 轮未达标
    且重检次数已达 2 次上限 → 停止。元数据含 2 条 refine 历史。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c-gamma", "gamma", document_id="ml-a")])
    refiner = _Refiner({})  # 任何 query 都原样返回（精化无效）
    service = KnowledgeService(
        index,
        refiner=refiner,
        relevance_threshold=1.0,
        max_refine_rounds=2,
    )

    result = service.adaptive_search("alpha", top_k=5)

    meta = result.metadata
    assert meta.threshold_met is False
    assert "上限" in meta.stopped_reason
    assert len(meta.rounds) == 3  # 首轮 + 2 次重检
    assert len(meta.refine_history) == 2  # 2 次精化，全部记录
    assert meta.refine_history == ("alpha", "alpha")
    assert meta.rounds[-1].top_score == 0.0
    # 精化器被调用 2 次（上限内），每次传入上一轮 top score 0.0。
    assert refiner.calls == [("alpha", 0.0), ("alpha", 0.0)]


def test_max_refine_rounds_zero_stops_immediately() -> None:
    """max_refine_rounds=0：首轮未达标即停止，精化器不被调用。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c-gamma", "gamma", document_id="ml-a")])
    refiner = _Refiner({"alpha": "gamma"})
    service = KnowledgeService(
        index,
        refiner=refiner,
        relevance_threshold=1.0,
        max_refine_rounds=0,
    )

    result = service.adaptive_search("alpha", top_k=5)

    assert result.metadata.threshold_met is False
    assert len(result.metadata.rounds) == 1
    assert result.metadata.refine_history == ()
    assert refiner.calls == []


def test_refiner_not_called_when_threshold_disabled() -> None:
    """阈值未启用（默认 None）时注入 refiner 也不触发重检。

    锁定「refiner 仅在阈值启用时生效」：阈值 None → 单轮返回，
    refiner 一次都不会被调用——避免无谓的重检成本，也是零回归
    语义的一部分（默认不注入阈值 = 不做任何重检）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    refiner = _Refiner({"support vector machine": "gamma"})
    service = KnowledgeService(index, refiner=refiner)

    result = service.adaptive_search("support vector machine", top_k=5)

    assert len(result.metadata.rounds) == 1
    assert result.metadata.threshold is None
    assert result.metadata.refine_history == ()
    assert result.metadata.stopped_reason == "未启用相关性阈值，单轮检索完成"
    assert refiner.calls == []  # refiner 从未被调用


def test_identity_refiner_reexamines_same_query() -> None:
    """IdentityQueryRefiner：重检原 query（结果必然相同）直到上限。

    显式注入 Identity 精化器 = 把「重检同一查询」走一遍；阈值 99.0
    永远不达标 → 3 轮后因上限停止（Identity 零回归语义）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    service = KnowledgeService(
        index,
        refiner=IdentityQueryRefiner(),
        relevance_threshold=99.0,
        max_refine_rounds=2,
    )

    result = service.adaptive_search("support vector machine", top_k=5)

    assert result.metadata.threshold_met is False
    assert "上限" in result.metadata.stopped_reason
    assert len(result.metadata.rounds) == 3
    assert result.metadata.refine_history == (
        "support vector machine",
        "support vector machine",
    )


# ── 4. 降级：refiner / policy 失败不抛错 ─────────────────────────


def test_refiner_exception_stops_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """精化器抛异常 → 停止重检，保留首轮结果，记录 warning。

    为什么不停下来降级为原 query 重检：同一索引、同一 query 重检
    必然得到相同结果（模块注释第 8 节第 3 点）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    broken = KnowledgeService(
        index,
        refiner=_BadRefiner("", error=RuntimeError("model unavailable")),
        relevance_threshold=5.0,
    )

    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        result = broken.adaptive_search("support vector machine", top_k=5)

    assert _ids_and_scores(result.hits) == (["c1"], [3.0])  # 首轮结果保留
    assert result.metadata.threshold_met is False
    assert result.metadata.stopped_reason == "重检器不可用，停止重检"
    assert len(result.metadata.rounds) == 1  # 没有重检轮
    assert "精化失败" in caplog.text


def test_refiner_blank_or_non_string_stops_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """精化器返回空白 / 非 str → 与抛异常同等对待（精化不可用）。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    blank = KnowledgeService(
        index, refiner=_BadRefiner("   "), relevance_threshold=5.0
    )
    non_string = KnowledgeService(
        index, refiner=_BadRefiner(None), relevance_threshold=5.0
    )

    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        result_blank = blank.adaptive_search("support vector machine", top_k=5)
        result_non_string = non_string.adaptive_search(
            "support vector machine", top_k=5
        )

    for result in (result_blank, result_non_string):
        assert result.metadata.threshold_met is False
        assert result.metadata.stopped_reason == "重检器不可用，停止重检"
        assert len(result.metadata.rounds) == 1
    assert "精化失败" in caplog.text


def test_policy_exception_falls_back_to_retrieve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """策略抛异常 → 降级为需要检索（宁可多检索，不可漏检）。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    broken = KnowledgeService(
        index, policy=_BadPolicy(None, error=RuntimeError("boom"))
    )

    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        result = broken.adaptive_search("support vector machine", top_k=5)

    assert result.metadata.needed is True
    assert result.metadata.need_reason == (
        "必要性判定失败（RuntimeError），默认需要检索"
    )
    assert _ids_and_scores(result.hits) == (["c1"], [3.0])
    assert "必要性判定失败" in caplog.text


def test_policy_invalid_return_falls_back_to_retrieve() -> None:
    """策略返回 None（违反协议）→ 与抛异常同等对待：需要检索。"""
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    broken = KnowledgeService(index, policy=_BadPolicy(None))

    result = broken.adaptive_search("support vector machine", top_k=5)

    assert result.metadata.needed is True
    assert "必要性判定失败" in result.metadata.need_reason
    assert _ids_and_scores(result.hits) == (["c1"], [3.0])


# ── 5. 必要性判定接入：简单问题不触发检索 ─────────────────────────


def test_no_retrieval_when_policy_says_no() -> None:
    """注入 HeuristicRetrievalPolicy：寒暄 → 不检索，元数据说明原因。

    验收标准第 1 条「简单问题直接作答，不触发检索」：hits 为空、
    needed=False、rounds 为空、need_reason 说明命中哪条规则。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    service = KnowledgeService(index, policy=HeuristicRetrievalPolicy())

    result = service.adaptive_search("你好", top_k=5)

    assert result.hits == []
    meta = result.metadata
    assert meta.needed is False
    assert meta.rounds == ()
    assert meta.threshold_met is None
    assert "问候" in meta.need_reason
    assert "无需检索" in meta.stopped_reason


# ── 6. 参数校验 ───────────────────────────────────────────────────


def test_threshold_and_rounds_validation() -> None:
    """阈值 ≤ 0 / 上限越界 → ValueError（服务构造与编排函数都校验）。

    上限须 ≥ 0 且 ≤ 10（> 10 是成本软上限，见 adaptive_search 的
    参数说明——每轮重检 = 一次完整检索，上限过高成本失控）。
    """
    with pytest.raises(ValueError):
        KnowledgeService(InMemoryKnowledgeIndex(), relevance_threshold=0)
    with pytest.raises(ValueError):
        KnowledgeService(InMemoryKnowledgeIndex(), relevance_threshold=-1.0)
    with pytest.raises(ValueError):
        KnowledgeService(InMemoryKnowledgeIndex(), max_refine_rounds=-1)
    with pytest.raises(ValueError):
        KnowledgeService(InMemoryKnowledgeIndex(), max_refine_rounds=11)
    with pytest.raises(ValueError):
        adaptive_search(InMemoryKnowledgeIndex(), "q", 5, relevance_threshold=0)
    with pytest.raises(ValueError):
        adaptive_search(InMemoryKnowledgeIndex(), "q", 5, max_refine_rounds=-1)
    with pytest.raises(ValueError):
        adaptive_search(InMemoryKnowledgeIndex(), "q", 5, max_refine_rounds=11)


def test_adaptive_search_validates_query_and_top_k() -> None:
    """空 query / top_k 越界 → ValueError（与 service.search 一致）。"""
    index = InMemoryKnowledgeIndex()
    with pytest.raises(ValueError):
        adaptive_search(index, "   ", 5)
    with pytest.raises(ValueError):
        adaptive_search(index, "q", 0)
    with pytest.raises(ValueError):
        adaptive_search(index, "q", 11)


# ── 7. 组合：rewriter / metadata_filter 透传 ──────────────────────


def test_adaptive_composes_with_rewriter() -> None:
    """改写器透传：阈值判定基于多变体合并后的 top score。

    构造（可手算，词法分数 = 命中英文词数）：变体 1 "support
    vector machine" 命中 c1 3 词 → 3.0；变体 2 "neural network"
    0 命中。合并 top score = 3.0 ≥ 阈值 2.0 → 达标（若阈值误判为
    单变体分数会得到错误结论，本用例锁定「判定基于合并结果」）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine", document_id="ml-a")])
    service = KnowledgeService(
        index,
        rewriter=_VariantRewriter(["support vector machine", "neural network"]),
        relevance_threshold=2.0,
    )

    result = service.adaptive_search("question", top_k=5)

    assert result.metadata.threshold_met is True
    assert result.metadata.rounds[0].top_score == 3.0
    assert _ids_and_scores(result.hits) == (["c1"], [3.0])


def test_adaptive_metadata_filter_applies() -> None:
    """过滤条件透传：被过滤的 chunk 不进结果、不参与阈值判定。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk(
                "c-keep",
                "support vector machine",
                document_id="d-1",
                source="s1",
            ),
            _chunk(
                "c-drop",
                "support vector machine",
                document_id="d-2",
                source="s2",
            ),
        ]
    )
    service = KnowledgeService(index, relevance_threshold=1.0)

    result = service.adaptive_search(
        "support vector machine", top_k=5, metadata_filter={"source": "s1"}
    )

    assert _ids_and_scores(result.hits) == (["c-keep"], [3.0])
    assert result.metadata.threshold_met is True
