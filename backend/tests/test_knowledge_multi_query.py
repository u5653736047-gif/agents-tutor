"""S4-T1 多路检索测试：Query 改写与多变体联合检索。

覆盖清单 A S4-T1 验收标准：
1. 多变体合并去重：两个变体命中重叠 chunk → 结果无重复、排序确定
   （分数降序、同分按 chunk_id 升序）；
2. 合并分 = max（chunk 在任一变体下的最高分），区别于「保留首个
   命中」——后出现的变体分数更高时取高分；
3. 降级路径：改写器抛异常 → 结果与原始 query 单路逐项一致（不抛错，
   且记录 warning 日志）；改写器返回空列表 / 全空白 → 同样降级；
4. 变体清洗：空白变体跳过、重复变体去重（同一变体不重复检索）；
5. 与混合检索组合：service 挂 HybridKnowledgeIndex + 多路检索 →
   每个变体各自走混合检索（词法失效的变体仍能借向量路命中，分数
   是 RRF 融合分而非词法分），合并去重；
6. metadata_filter 组合：过滤条件透传到每个变体，被过滤的 chunk
   不进入任何变体、自然不进合并结果；
7. 零回归：默认不改写（IdentityQueryRewriter）时，service.search
   与直接调用索引 search 逐项一致（分数、顺序、citation）。
"""
from __future__ import annotations

import logging

import pytest

from core.knowledge.embedding import HashEmbeddingProvider
from core.knowledge.hybrid import HybridKnowledgeIndex
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, SearchHit
from core.knowledge.retrieval import multi_query_search
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import InMemoryVectorKnowledgeIndex

# ── 小工具与测试替身 ──────────────────────────────────────────────


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
    source: str | None = None,
    metadata: dict[str, object] | None = None,
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
        metadata=metadata or {},
    )


class _VariantRewriter:
    """改写器测试替身：返回固定变体列表，可配置抛错；记录调用。

    满足 retrieval.QueryRewriter 协议（鸭子类型）：只要提供
    rewrite(query) -> list[str] 方法即可被 KnowledgeService 接受。
    """

    def __init__(
        self,
        variants: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self._variants = variants
        self._error = error
        self.calls: list[str] = []

    def rewrite(self, query: str) -> list[str]:
        self.calls.append(query)
        if self._error is not None:
            raise self._error
        # 直接返回构造时传入的列表（不做 list() 拷贝）：测试可以传入
        # None 或含非字符串元素的列表，模拟改写器违反协议的情况。
        return self._variants


def _ids_and_scores(hits: list[SearchHit]) -> tuple[list[str], list[float]]:
    """把检索结果压成 (chunk_id 列表, score 列表)，便于逐项断言。"""
    return (
        [hit.chunk.chunk_id for hit in hits],
        [hit.score for hit in hits],
    )


# ── 1. 多变体合并去重 + 排序确定 ──────────────────────────────────


def test_multi_variant_merge_deduplicates_and_sorts() -> None:
    """两个变体命中重叠 chunk：去重、取 max 分、排序确定。

    构造（全部可手算，词法分数 = 命中英文词数）：
    - c-svm "support vector machine"：变体 1 命中 3 词 → 3 分；
    - c-nn  "neural network"：变体 2 命中 2 词 → 2 分；
    - c-both "support vector machine neural network"：两个变体都命中
      → 变体 1 下 3 分、变体 2 下 2 分，max 合并取 3 分。
    若不去重，c-both 会出现两次；若按「首个命中」合并，c-both 只得
    变体 1 的 3 分（本例恰好相同）——去重与 max 的正确性由
    test_merge_takes_max_score_not_first_hit 单独锁定。
    排序：分数降序（3, 3, 2），同分按 chunk_id 升序（c-both < c-svm）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c-svm", "support vector machine", document_id="ml-a"),
            _chunk("c-nn", "neural network", document_id="ml-b"),
            _chunk(
                "c-both", "support vector machine neural network", document_id="ml-c"
            ),
        ]
    )
    rewriter = _VariantRewriter(["support vector machine", "neural network"])
    service = KnowledgeService(index, rewriter=rewriter)

    hits = service.search("user question", top_k=10)

    ids, scores = _ids_and_scores(hits)
    assert ids == ["c-both", "c-svm", "c-nn"]
    assert scores == [3.0, 3.0, 2.0]
    # 去重：同一 chunk_id 只出现一次。
    assert len(ids) == len(set(ids))
    # 原 query 传给改写器（改写器拿到的是用户问题，不是变体）。
    assert rewriter.calls == ["user question"]


def test_merge_takes_max_score_not_first_hit() -> None:
    """合并分取 max 而非「保留首个命中」：后出现的变体分数更高时取高分。

    c-x "neural network support" 被两个变体命中：变体 1（先出现）
    得 1 分（只命中 support），变体 2（后出现）得 2 分（命中 neural、
    network）。「保留首个命中」会错误地给 1 分；max 合并给 2 分。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c-x", "neural network support")])
    rewriter = _VariantRewriter(["support vector machine", "neural network"])
    service = KnowledgeService(index, rewriter=rewriter)

    hits = service.search("question", top_k=10)

    assert len(hits) == 1
    assert hits[0].chunk.chunk_id == "c-x"
    assert hits[0].score == 2.0


# ── 2. 降级路径（改写失败不阻断检索）──────────────────────────────


def test_rewriter_exception_falls_back_to_raw_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """改写器抛异常 → 降级为原始 query 单路，结果与默认服务逐项一致。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c1", "support vector machine", document_id="ml-a"),
            _chunk("c2", "support", document_id="ml-b"),
        ]
    )
    baseline = KnowledgeService(index)
    broken = KnowledgeService(
        index,
        rewriter=_VariantRewriter([], error=RuntimeError("model unavailable")),
    )

    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        expected = baseline.search("support vector", top_k=5)
        actual = broken.search("support vector", top_k=5)

    expected_ids, expected_scores = _ids_and_scores(expected)
    actual_ids, actual_scores = _ids_and_scores(actual)
    assert actual_ids == expected_ids
    assert actual_scores == expected_scores
    # citation 逐项一致（document_id / source / chunk_id）。
    assert [hit.citation.model_dump() for hit in actual] == [
        hit.citation.model_dump() for hit in expected
    ]
    # 降级被记录（warning 日志），便于调试时看到「改写失败但检索照常」。
    assert "降级" in caplog.text


def test_rewriter_empty_or_blank_variants_fall_back() -> None:
    """改写器返回空列表 / 全空白 → 同样降级为原始 query 单路。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c1", "support vector machine"),
            _chunk("c2", "neural network"),
        ]
    )
    baseline = KnowledgeService(index)
    empty = KnowledgeService(index, rewriter=_VariantRewriter([]))
    blank = KnowledgeService(index, rewriter=_VariantRewriter(["  ", ""]))

    expected = baseline.search("support vector", top_k=5)
    assert _ids_and_scores(empty.search("support vector", top_k=5)) == _ids_and_scores(
        expected
    )
    assert _ids_and_scores(blank.search("support vector", top_k=5)) == _ids_and_scores(
        expected
    )


def test_blank_and_duplicate_variants_are_cleaned() -> None:
    """空白变体跳过、重复变体去重：["q", "", "q"] 等价于单变体 ["q"]。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c1", "support vector machine"),
            _chunk("c2", "neural network"),
        ]
    )
    rewriter = _VariantRewriter(["support vector", "", "support vector"])
    service = KnowledgeService(index, rewriter=rewriter)

    hits = service.search("question", top_k=10)

    # 清洗后只剩一个变体：结果与直接用该变体检索一致（c2 不命中）。
    assert _ids_and_scores(hits) == (["c1"], [2.0])
    # 改写器只被调用一次（去重在编排层，不在改写器内部）。
    assert rewriter.calls == ["question"]


def test_rewriter_returning_none_falls_back() -> None:
    """改写器违反协议返回 None → 降级为原始 query 单路，不抛错。

    改写器是外部组件：返回 None 与抛异常同等对待（改写不可用），
    检索不应被可选的增强阻断（可用性优先，见模块注释第 5 节）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c1", "support vector machine"),
            _chunk("c2", "neural network"),
        ]
    )
    baseline = KnowledgeService(index)
    broken = KnowledgeService(
        index,
        rewriter=_VariantRewriter(None),  # type: ignore[arg-type]
    )

    expected = baseline.search("support vector", top_k=5)
    actual = broken.search("support vector", top_k=5)

    assert _ids_and_scores(actual) == _ids_and_scores(expected)
    assert [hit.citation.model_dump() for hit in actual] == [
        hit.citation.model_dump() for hit in expected
    ]


def test_rewriter_returning_non_string_variants_falls_back() -> None:
    """改写器返回含非字符串元素的列表 → 降级为原始 query 单路，不抛错。

    若清洗逻辑不在 try 内（或没有类型校验），None 元素会在 strip()
    处抛 AttributeError 并传播出检索——本用例锁定「类型不合法同样
    降级」的语义（见模块注释第 5 节）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine")])
    baseline = KnowledgeService(index)
    broken = KnowledgeService(
        index,
        rewriter=_VariantRewriter(["neural network", None]),  # type: ignore[list-item]
    )

    assert _ids_and_scores(broken.search("support", top_k=5)) == _ids_and_scores(
        baseline.search("support", top_k=5)
    )


def test_blank_query_does_not_trigger_fallback_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """空白 query 不是改写失败：直接返回空结果，不打「降级」warning。

    直接调用导出函数 multi_query_search（绕过 service 的输入校验）：
    空白 query 时索引层本来就返回空结果，不应误报「查询改写结果
    为空」（见 _safe_variants 的短路处理）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine")])
    rewriter = _VariantRewriter(["gamma"])

    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        hits = multi_query_search(index, "   ", top_k=5, rewriter=rewriter)

    assert hits == []
    assert "降级" not in caplog.text
    # 空白 query 直接短路返回，改写器根本不会被调用。
    assert rewriter.calls == []


# ── 3. 与混合检索组合 ─────────────────────────────────────────────


def test_variants_each_go_through_hybrid_index() -> None:
    """每个变体各自走混合检索：词法失效的变体仍能借向量路命中。

    构造（复用 S3-T5 的同义替身思路）：
    - provider 把「土豆」归一化为「马铃薯」（模拟语义模型的同义映射）；
    - c-potato "马铃薯"：变体 1 "土豆" 词法 0 命中（词素无交集），
      但向量路 normalize 后余弦 1.0 → 变体 1 的混合结果命中它
      （RRF 单项分 1/61 兜底）；
    - c-lin "linear regression"：变体 2 词法与向量都命中 → RRF 分
      1/61 + 1/61 = 2/61。
    若每变体没有走混合（只走词法），变体 1 会 0 命中、c-potato 丢失；
    若合并逻辑错误（如保留首个），分数也会不同。断言分数为 RRF
    融合分（≈0.0328、≈0.0164），证明「变体 → 混合 → 合并」链路完整。
    """
    provider = HashEmbeddingProvider(
        normalize=lambda text: text.replace("土豆", "马铃薯")
    )
    hybrid = HybridKnowledgeIndex(
        InMemoryKnowledgeIndex(),
        InMemoryVectorKnowledgeIndex(provider),
    )
    hybrid.upsert(
        [
            _chunk("c-potato", "马铃薯", document_id="veg"),
            _chunk("c-lin", "linear regression", document_id="ml"),
        ]
    )
    rewriter = _VariantRewriter(["土豆", "linear regression"])
    service = KnowledgeService(hybrid, rewriter=rewriter)

    hits = service.search("哪个网络更适合分类", top_k=10)

    ids, scores = _ids_and_scores(hits)
    assert ids == ["c-lin", "c-potato"]
    assert scores[0] == pytest.approx(2 / 61)  # RRF 融合分（两路都第 1 名）
    assert scores[1] == pytest.approx(1 / 61)  # 只有向量路命中的兜底分
    assert len(ids) == len(set(ids))


# ── 4. metadata_filter 组合 ───────────────────────────────────────


def test_metadata_filter_applies_to_every_variant() -> None:
    """过滤条件透传到每个变体：被过滤的 chunk 不进任何变体。

    构造：
    - c-a "support"、c-both "support neural network" 属于 s1（显式指定
      source="s1"）；
    - c-b "neural network" 属于 s2（过滤条件 {"source": "s1"} 排除它）。
    变体 1 "support" 命中 c-a、c-both（1 分、1 分）；变体 2
    "neural network" 命中 c-both（2 分，c-b 被过滤）。合并后
    c-both 取 max(1, 2) = 2 分排第一，c-a 1 分第二，c-b 不出现。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c-a", "support", document_id="d-a", source="s1"),
            _chunk("c-b", "neural network", document_id="d-b", source="s2"),
            _chunk(
                "c-both", "support neural network", document_id="d-both", source="s1"
            ),
        ]
    )
    rewriter = _VariantRewriter(["support", "neural network"])
    service = KnowledgeService(index, rewriter=rewriter)

    hits = service.search(
        "question", top_k=10, metadata_filter={"source": "s1"}
    )

    ids, scores = _ids_and_scores(hits)
    assert ids == ["c-both", "c-a"]
    assert scores == [2.0, 1.0]
    # 对照：不过滤时 c-b 也会入选——证明过滤确实透传到了每个变体。
    unfiltered = service.search("question", top_k=10)
    assert "c-b" in _ids_and_scores(unfiltered)[0]


# ── 5. 零回归：默认不改写与直接索引检索一致 ───────────────────────


def test_default_identity_matches_direct_index_search() -> None:
    """默认不改写（IdentityQueryRewriter）：与直接调用索引 search 逐项一致。

    包含同分情形：c1/c2 都命中 3 个词（同分），平局规则按 chunk_id
    升序（c1 < c2）；并逐项断言分数与 citation 完整性——锁定 S3
    行为零回归。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c2", "support vector machine", document_id="ml-b"),
            _chunk("c1", "support vector machine", document_id="ml-a"),
            _chunk("c3", "neural network", document_id="ml-c"),
        ]
    )
    service = KnowledgeService(index)

    direct = index.search("support vector machine", top_k=5)
    via_service = service.search("support vector machine", top_k=5)

    assert [hit.chunk.chunk_id for hit in via_service] == [
        hit.chunk.chunk_id for hit in direct
    ]
    assert [hit.score for hit in via_service] == [hit.score for hit in direct]
    assert [hit.citation.model_dump() for hit in via_service] == [
        hit.citation.model_dump() for hit in direct
    ]


def test_default_identity_matches_multi_query_search_no_rewriter() -> None:
    """multi_query_search 的 rewriter=None 与显式 Identity 完全等价。"""
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c1", "support vector machine"),
            _chunk("c2", "neural network"),
        ]
    )

    explicit = multi_query_search(index, "support", top_k=5)
    defaulted = multi_query_search(
        index, "support", top_k=5, rewriter=None
    )

    assert _ids_and_scores(explicit) == _ids_and_scores(defaulted)
    assert [hit.citation.model_dump() for hit in explicit] == [
        hit.citation.model_dump() for hit in defaulted
    ]


# ── 6. 改写器可注入、可替换 ───────────────────────────────────────


def test_rewriter_is_injectable_and_replaces_default() -> None:
    """注入的改写器生效：结果按改写后的变体检索，默认实现被替换。

    改写器把任何 query 都改写为 "gamma"——若注入未生效（仍走默认
    Identity），"alpha" 原样检索会命中 c1；注入生效后只命中 c-gamma。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c1", "alpha"),
            _chunk("c-gamma", "gamma"),
        ]
    )
    rewriter = _VariantRewriter(["gamma"])
    service = KnowledgeService(index, rewriter=rewriter)

    hits = service.search("alpha", top_k=10)

    assert _ids_and_scores(hits) == (["c-gamma"], [1.0])
    assert rewriter.calls == ["alpha"]
