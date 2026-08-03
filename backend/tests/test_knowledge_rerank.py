"""S4-T2 重排序测试：Reranker 协议、Identity 零回归、替身重排器与降级。

覆盖清单 A S4-T2 验收标准：
1. 初检 Top-N 经重排器输出最终 Top-K：重排发生在「初检合并」与
   「最终截断」之间；重排器收到候选窗口 max(top_k×2, 10) 而非已
   截断的 top_k；Top-K 截断在重排之后；
2. 重排提供方可替换：注入替身重排器生效、可换实例（测试用替身，
   确定性逻辑见 _OverlapReranker）；
3. 首位命中率优于融合排序基线：构造「初检首位不是正确答案、重排
   器把正确答案提到首位」的场景，断言基线 vs 重排后首位不同、且
   重排后首位为期望项；
4. 组合与降级：
   - Identity 零回归：未注入与注入 IdentityReranker 结果逐项一致，
     且与直接调用索引 search 一致；
   - 替身重排器确定性：同一输入跑两遍，顺序与分数逐项一致；同分
     平局按初检相对顺序裁决（不依赖 chunk_id 巧合）；
   - 与多路检索组合：重排在多变体合并去重之后执行（候选无重复、
     query 是原始用户问题）；
   - 与 metadata_filter 组合：过滤先于初检与重排（被过滤的 chunk
     不进重排器看到的候选）；
   - 降级：重排器抛异常 / 返回类型不合法 → 保持初检结果，不抛错，
     记录 warning；重排器合法返回空列表 → 透传空结果（与改写器
     降级刻意不对称，见模块注释第 7 节第 5 点）。
"""
from __future__ import annotations

import logging

import pytest

from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import Citation, KnowledgeChunk, SearchHit
from core.knowledge.retrieval import IdentityReranker
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


def _hit(chunk_id: str, content: str, score: float) -> SearchHit:
    """按 chunk 构造一个 SearchHit（单测替身 rerank 时直接传 hits 用）。

    只用于绕过检索链路直接调用 _OverlapReranker.rerank 的用例：
    score 表达「初检分数」（列表顺序 = 初检相对顺序），替身重排
    不修改 score。
    """
    chunk = _chunk(chunk_id, content)
    return SearchHit(
        chunk=chunk,
        citation=Citation(
            document_id=chunk.document_id,
            source=chunk.source,
            page=None,
            chunk_id=chunk_id,
        ),
        score=score,
    )


class _VariantRewriter:
    """改写器测试替身：返回固定变体列表（与 S4-T1 测试同一思路）。"""

    def __init__(self, variants: list[str]) -> None:
        self._variants = variants

    def rewrite(self, query: str) -> list[str]:
        return self._variants


class _OverlapReranker:
    """重排器测试替身：按 query 与 content 的英文词重合度重新打分排序。

    原理（面向初学者）：把 query 拆成小写英文单词集合，对每个候选
    hit 统计「query 中的词在 content 里出现了多少个」作为重排分，
    按重排分降序排列；重排分相同时保持初检相对顺序（保证确定性）。
    这个替身模拟真实重排器的「重新审视相关性」：初检分数来自词法
    命中数（检索层面），重排分来自与用户问题的重合度（语义层面），
    两者可以不一致——从而演示「重排改变了顺序」。它是确定性的：
    同样的输入永远得到同样的输出，测试可以精确断言。

    注意：替身只重排顺序、不改 score（协议不强制更新分数，见
    retrieval.py 模块注释第 7 节第 1 点）；真实 Cross-Encoder 会
    在 rerank() 里更新 score，但流程位置与协议不变。
    """

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        # 记录每次调用：(原始 query, 收到的候选数, top_k)——测试据此
        # 断言「重排器拿到的是原始问题 + 候选窗口 + 透传的 top_k」。
        self.calls: list[tuple[str, int, int]] = []

    def rerank(
        self, query: str, hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        self.calls.append((query, len(hits), top_k))
        if self._error is not None:
            raise self._error
        # 只统计纯英文单词（isalpha 过滤掉 "term-0" 这类含数字的词），
        # 避免测试数据里的连字符/数字干扰重合度计算。
        query_words = {word for word in query.lower().split() if word.isalpha()}
        # 三元组 (重合度, 初检序号, hit)：重合度降序、同分按初检序号
        # 升序——初检顺序是平局裁决，结果确定、可复现。
        scored = [
            (
                sum(1 for word in query_words if word in hit.chunk.content.lower()),
                i,
                hit,
            )
            for i, hit in enumerate(hits)
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [hit for _, _, hit in scored]


class _BadReranker:
    """返回不合法值的重排器替身：模拟外部组件违反协议（返回 None 等）。

    声明签名符合 Reranker 协议（鸭子类型），实际返回构造时传入的
    任意值——测试据此锁定「类型不合法同样降级」的语义。
    """

    def __init__(self, result: object) -> None:
        self._result = result

    def rerank(
        self, query: str, hits: list[SearchHit], top_k: int
    ) -> list[SearchHit]:
        return self._result  # type: ignore[return-value]


def _ids_and_scores(hits: list[SearchHit]) -> tuple[list[str], list[float]]:
    """把检索结果压成 (chunk_id 列表, score 列表)，便于逐项断言。"""
    return (
        [hit.chunk.chunk_id for hit in hits],
        [hit.score for hit in hits],
    )


# ── 1. 重排生效：首位命中率优于融合排序基线 ───────────────────────


def test_rerank_moves_correct_answer_to_first() -> None:
    """构造「初检首位不是正确答案」场景：重排后首位变为期望项。

    场景（验收标准第 3 条，全部可手算，词法分数 = 命中英文词数）：
    - 用户问题 "neural networks"；
    - 改写器把问题改写为两个变体 ["support vector machine",
      "neural network"]；
    - c-wrong "support vector machine"：变体 1 命中 3 词 → 初检合并
      分 3.0，排第一（融合排序基线的首位）；
    - c-right "neural network"：变体 2 命中 2 词 → 初检合并分 2.0，
      排第二——正确答案没进基线首位。
    但替身重排器按「与用户问题重合度」打分：c-right 重合 1 词
    （neural）> c-wrong 重合 0 词，重排后提到首位。
    断言：
    1. 基线（不注入重排器）首位是 c-wrong；
    2. 注入替身重排器后首位是 c-right——与基线首位不同、且为期望项，
       即「重排后首位命中率优于融合排序基线」。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c-wrong", "support vector machine", document_id="ml-a"),
            _chunk("c-right", "neural network", document_id="ml-b"),
        ]
    )
    rewriter = _VariantRewriter(["support vector machine", "neural network"])
    baseline = KnowledgeService(index, rewriter=rewriter)
    reranked = KnowledgeService(
        index, rewriter=rewriter, reranker=_OverlapReranker()
    )

    base_hits = baseline.search("neural networks", top_k=5)
    new_hits = reranked.search("neural networks", top_k=5)

    # 基线首位是初检第一名（c-wrong）——「正确答案没进首位」成立。
    assert base_hits[0].chunk.chunk_id == "c-wrong"
    # 重排后首位变为 c-right：与基线首位不同，且是期望的正确答案。
    assert new_hits[0].chunk.chunk_id == "c-right"
    assert new_hits[0].chunk.chunk_id != base_hits[0].chunk.chunk_id


def test_rerank_happens_before_final_truncation() -> None:
    """Top-K 截断发生在重排之后：top_k=1 时返回重排后的第 1 名。

    若截断在重排前（初检先截 1 名再重排），重排器只能看到 c-a，
    c-b 永远没有机会；重排先于截断时，c-b 被提到首位并被保留。
    这锁定「初检 Top-N → 重排 → 截断最终 Top-K」的流程顺序
    （验收标准第 1 条）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c-a", "support vector machine", document_id="ml-a"),
            _chunk("c-b", "neural network", document_id="ml-b"),
        ]
    )
    rewriter = _VariantRewriter(["support vector machine", "neural network"])
    service = KnowledgeService(
        index, rewriter=rewriter, reranker=_OverlapReranker()
    )

    hits = service.search("neural networks", top_k=1)

    # 初检合并：c-a 3.0 第一；重排后 c-b（重合 1 词）> c-a（0 词）；
    # 截断发生在重排后 → 只返回 c-b。
    assert [hit.chunk.chunk_id for hit in hits] == ["c-b"]


def test_reranker_receives_candidate_window_larger_than_top_k() -> None:
    """重排器收到的是候选窗口 max(top_k×2, 10)，而非已截断的 top_k。

    构造 12 个 chunk：top_k=1 时候选窗口是 10，重排器应收到 10 个
    候选而不是初检第 1 名。若实现把截断放在重排前，传入长度会是
    1——本用例锁定「重排前留候选窗口」（模块注释第 7 节第 2 点）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk(f"c-{i:02d}", f"term-{i}", document_id=f"d-{i}")
            for i in range(12)
        ]
    )
    reranker = _OverlapReranker()
    service = KnowledgeService(index, reranker=reranker)

    service.search("term-0", top_k=1)

    # 三元组 (query, 候选数, top_k)：top_k=1 原样传入重排器。
    assert reranker.calls == [("term-0", 10, 1)]


# ── 2. Identity 零回归 ────────────────────────────────────────────


def test_identity_reranker_zero_regression() -> None:
    """未注入重排器与注入 IdentityReranker 结果完全一致（零回归）。

    锁定「不注入 = Identity（默认不重排）」，且与直接调用索引 search
    逐项一致（S3 行为零回归）：候选窗口 ≥ top_k，先截候选再截 top_k
    与直接截 top_k 等价——重排步骤对默认行为零影响（模块注释第 7 节
    第 3 点）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("c2", "support vector machine", document_id="ml-b"),
            _chunk("c1", "support vector machine", document_id="ml-a"),
            _chunk("c3", "neural network", document_id="ml-c"),
        ]
    )
    plain = KnowledgeService(index)
    identity = KnowledgeService(index, reranker=IdentityReranker())

    expected = plain.search("support vector machine", top_k=5)
    actual = identity.search("support vector machine", top_k=5)

    assert _ids_and_scores(actual) == _ids_and_scores(expected)
    assert [hit.citation.model_dump() for hit in actual] == [
        hit.citation.model_dump() for hit in expected
    ]
    # 与直接调用索引 search 也逐项一致（S3 行为零回归）。
    direct = index.search("support vector machine", top_k=5)
    assert _ids_and_scores(actual) == _ids_and_scores(direct)


# ── 3. 替身重排器确定性 ───────────────────────────────────────────


def test_standin_reranker_is_deterministic() -> None:
    """替身重排器确定性 + 同分平局按初检相对顺序裁决。

    直接单测 _OverlapReranker.rerank（绕过检索链路）：端到端链路里
    词法 0 分 chunk 会被索引跳过（index.py 的 score ≤ 0 语义），
    构造「初检顺序与 chunk_id 相反的同分」数据会受链路巧合干扰；
    这里把 hits 按「初检顺序」直接传入，只锁定替身自身的行为。

    平局裁决：传入 hits=[c-b, c-a]（c-b 初检在前，与 chunk_id 序
    [c-a, c-b] 相反），两者与 query "alpha" 的重合分同为 1。若平局
    规则误按 chunk_id 升序会得到 [c-a, c-b]；正确实现按传入顺序
    （初检相对顺序）保持 [c-b, c-a]。

    确定性：同一输入跑两遍，输出逐项一致（重排器是外部组件，测试
    必须能精确断言它的行为）。
    """
    c_a = _hit("c-a", "alpha", score=1.0)
    c_b = _hit("c-b", "alpha", score=2.0)
    reranker = _OverlapReranker()

    first = reranker.rerank("alpha", [c_b, c_a], top_k=5)
    second = reranker.rerank("alpha", [c_b, c_a], top_k=5)

    # 平局裁决：同分（重合 1 = 1）保持传入顺序 [c-b, c-a]，
    # 而不是按 chunk_id 升序 [c-a, c-b]。
    assert [hit.chunk.chunk_id for hit in first] == ["c-b", "c-a"]
    # 确定性：两遍输出逐项一致（顺序 + 分数 + citation）。
    assert _ids_and_scores(first) == _ids_and_scores(second)
    assert [hit.citation.model_dump() for hit in first] == [
        hit.citation.model_dump() for hit in second
    ]


# ── 4. 与多路检索组合 ─────────────────────────────────────────────


def test_rerank_composes_with_multi_query_after_merge() -> None:
    """与多路检索组合：重排在多变体合并去重之后执行。

    断言：
    - 重排器收到的候选列表无重复 chunk_id（合并去重先发生）；
    - query 是原始用户问题（不是检索变体）；
    - 重排把初检第 3 名（c-nn，与用户问题重合度最高）提到首位。
    构造（词法分数 = 命中英文词数）：
    - c-both "support vector machine neural network"：变体 1 命中
      3 词、变体 2 命中 2 词 → 合并分 3.0（初检首位）；
    - c-svm  "support vector machine"：变体 1 命中 3 词 → 3.0；
    - c-nn   "neural networks"：变体 2 命中 1 词（neural）→ 1.0
      （初检第 3 名，基线首位看不到它）。
    重排分（query "neural networks" 重合词数）：c-nn 2 > c-both 1
    > c-svm 0 → 重排后 [c-nn, c-both, c-svm]。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk(
                "c-both",
                "support vector machine neural network",
                document_id="ml-a",
            ),
            _chunk("c-svm", "support vector machine", document_id="ml-b"),
            _chunk("c-nn", "neural networks", document_id="ml-c"),
        ]
    )
    rewriter = _VariantRewriter(["support vector machine", "neural network"])
    reranker = _OverlapReranker()
    service = KnowledgeService(index, rewriter=rewriter, reranker=reranker)

    hits = service.search("neural networks", top_k=10)

    # 合并去重先发生：三个 chunk 进候选窗口，无重复。
    (query_seen, n_candidates, seen_top_k), *_ = reranker.calls
    assert query_seen == "neural networks"  # 原始用户问题，不是变体
    assert n_candidates == 3
    assert seen_top_k == 10  # top_k 原样透传给重排器
    assert len(hits) == 3
    assert len({hit.chunk.chunk_id for hit in hits}) == 3
    # 重排后首位是与用户问题重合度最高的 c-nn（初检首位是 c-both）。
    assert hits[0].chunk.chunk_id == "c-nn"


# ── 5. 与 metadata_filter 组合 ────────────────────────────────────


def test_metadata_filter_applies_before_initial_search_and_rerank() -> None:
    """过滤先于初检与重排：被过滤的 chunk 不会出现在重排器看到的候选中。

    构造：
    - c-keep "support vector machine neural networks" 属于 s1；
    - c-drop 内容与 c-keep 完全相同但属于 s2——若过滤失效或发生在
      重排之后，重排器会收到它。
    带 {"source": "s1"} 检索：c-drop 在初检阶段就被排除，重排器只
    收到 c-keep 一个候选（过滤 → 初检 → 重排的顺序被锁定）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk(
                "c-keep",
                "support vector machine neural networks",
                document_id="d-1",
                source="s1",
            ),
            _chunk(
                "c-drop",
                "support vector machine neural networks",
                document_id="d-2",
                source="s2",
            ),
        ]
    )
    reranker = _OverlapReranker()
    service = KnowledgeService(index, reranker=reranker)

    hits = service.search(
        "neural networks", top_k=5, metadata_filter={"source": "s1"}
    )

    # query "neural networks" 命中 c-keep 2 词 → 2.0 分；c-drop 被过滤。
    assert _ids_and_scores(hits) == (["c-keep"], [2.0])
    # 重排器只收到过滤后的候选（c-drop 进不了重排）。
    (query_seen, n_candidates, seen_top_k), *_ = reranker.calls
    assert query_seen == "neural networks"
    assert n_candidates == 1
    assert seen_top_k == 5  # top_k 原样透传给重排器


# ── 6. 降级语义（重排失败不阻断检索）──────────────────────────────


def test_reranker_exception_keeps_initial_results(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重排器抛异常 → 保持初检结果，不抛错，记录 warning。

    与改写降级同一哲学（可用性优先）：重排是可选的增强，任何失败
    都不应阻断检索（模块注释第 7 节第 5 点）。
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
        index, reranker=_OverlapReranker(error=RuntimeError("model unavailable"))
    )

    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        expected = baseline.search("support vector", top_k=5)
        actual = broken.search("support vector", top_k=5)

    assert _ids_and_scores(actual) == _ids_and_scores(expected)
    assert [hit.citation.model_dump() for hit in actual] == [
        hit.citation.model_dump() for hit in expected
    ]
    assert "重排失败" in caplog.text


def test_reranker_invalid_return_keeps_initial_results(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重排器返回 None / 含非 SearchHit 元素 → 保持初检结果，不抛错。

    重排器是外部组件：返回类型不合法与抛异常同等对待（重排不可用），
    检索不应被可选的增强阻断；降级同样记录 warning（与异常降级用例
    对称）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine")])
    baseline = KnowledgeService(index)
    none_reranker = KnowledgeService(index, reranker=_BadReranker(None))
    mixed_reranker = KnowledgeService(
        index, reranker=_BadReranker(["not a hit"])
    )

    expected = baseline.search("support", top_k=5)
    with caplog.at_level(logging.WARNING, logger="core.knowledge.retrieval"):
        actual_none = none_reranker.search("support", top_k=5)
        actual_mixed = mixed_reranker.search("support", top_k=5)

    assert _ids_and_scores(actual_none) == _ids_and_scores(expected)
    assert _ids_and_scores(actual_mixed) == _ids_and_scores(expected)
    # 类型不合法同样触发降级 warning（与异常降级用例对称）。
    assert "重排失败" in caplog.text


def test_reranker_empty_return_returns_empty_results() -> None:
    """重排器合法返回空列表 → 返回空结果（不降级为初检候选）。

    与改写器降级刻意不对称（模块注释第 7 节第 5 点）：改写器返回空
    通常意味着改写器异常，降级为原始 query；重排器返回空是「裁决
    候选均不相关」的合法结果，直接透传空列表——若降级为初检候选
    反而违背「重排结果说了算」的意图。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("c1", "support vector machine")])
    service = KnowledgeService(index, reranker=_BadReranker([]))

    assert service.search("support", top_k=5) == []


# ── 7. 重排器可注入、可替换 ───────────────────────────────────────


def test_reranker_is_injectable_and_replaces_default() -> None:
    """注入的重排器生效、可替换：替身记录到调用即证明注入生效。

    与改写器可注入同一论证（S4-T1）：协议 + 构造注入，调用方不需要
    改检索层任何代码；换一个替身实例同样生效（可替换）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            # c1 含 "network"，query "neural network" 对其也有词法命中
            # （1 词）；c2 命中 2 词——两个候选都进入重排窗口，替身
            # 记录到候选数 2（若 c1 与 query 无交集，候选只有 1 个，
            # 断言会与实际不符）。
            _chunk("c1", "network topology"),
            _chunk("c2", "neural network"),
        ]
    )

    first_reranker = _OverlapReranker()
    first = KnowledgeService(index, reranker=first_reranker)
    first.search("neural network", top_k=5)
    assert first_reranker.calls == [("neural network", 2, 5)]

    # 替换成另一个替身实例：同样生效（协议可替换）。
    second_reranker = _OverlapReranker()
    second = KnowledgeService(index, reranker=second_reranker)
    second.search("neural network", top_k=5)
    assert second_reranker.calls == [("neural network", 2, 5)]
