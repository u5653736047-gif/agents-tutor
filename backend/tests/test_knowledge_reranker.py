"""S5 FastEmbedReranker（Cross-Encoder 重排器）单元测试。

覆盖清单（生产注入组件的全分支覆盖）：
1. 正常路径：按打分器重排候选（初检首位非最优被纠正）；返回长度
   与候选一致（截断由检索层执行）；保留原始 score 字段不改写
   （reranker.py 模块注释第 3 节的分数契约）；top_k 参数不消费；
2. 平局与确定性：重排分相同按 chunk_id 升序（与索引层平局规则
   一致）；同一输入两次调用输出逐项一致；
3. 边界与异常：空候选直接返回空且不打分；打分器返回长度不符 /
   非数值（含 bool）→ ValueError（由检索层 _safe_rerank 降级，本
   模块测试只锁定「抛错」契约）；model_name 空白构造期拒绝；
4. 真实打分函数装配：默认构造路径经 _load_cross_encoder_class
   加载 encoder（测试注入替身类，零模型零网络）；fastembed 未安装
   （加载抛 RuntimeError）→ 构造期上抛 RuntimeError；
5. 与检索链路集成：注入 KnowledgeService 后 search 的最终顺序
   由重排决定，且原 RRF 分数透传不改写。
"""

from __future__ import annotations

from typing import Any

import pytest

import core.knowledge.reranker as reranker_module
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import Citation, KnowledgeChunk, SearchHit
from core.knowledge.reranker import BatchScorer, FastEmbedReranker
from core.knowledge.service import KnowledgeService


def _hit(chunk_id: str, content: str, score: float) -> SearchHit:
    """构造携带指定初检分数的 SearchHit（与 test_knowledge_rerank.py 同形态）。"""
    chunk = KnowledgeChunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        content=content,
        source="doc-1.txt",
        page=None,
        start=0,
        end=len(content),
        metadata={},
    )
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


def _scorer_by_keyword(keyword: str) -> BatchScorer:
    """确定性打分替身：候选正文含 keyword 得 1.0，否则 0.0。"""

    def score(query: str, documents: list[str]) -> list[float]:
        return [1.0 if keyword in document else 0.0 for document in documents]

    return score


# ── 1. 正常路径：重排生效且保留原分数 ─────────────────────────────


def test_rerank_reorders_by_scorer_and_preserves_original_scores() -> None:
    """打分器把初检末位提到首位；每个 hit 的 score 保持初检原值。"""
    hits = [
        _hit("c-a", "支持向量机", score=3.0),
        _hit("c-b", "过拟合与正则化", score=1.0),
    ]
    reranker = FastEmbedReranker(_scorer_by_keyword("正则化"))

    result = reranker.rerank("怎么防止过拟合", hits, top_k=5)

    # 顺序被重排（c-b 提到首位），但 score 保留初检原值不改写。
    assert [hit.chunk.chunk_id for hit in result] == ["c-b", "c-a"]
    assert [hit.score for hit in result] == [1.0, 3.0]
    # 返回完整重排列表（截断 top_k 由检索层在重排后统一执行）。
    assert len(result) == len(hits)


def test_rerank_is_deterministic_with_chunk_id_tiebreak() -> None:
    """同分平局按 chunk_id 升序（与索引层平局规则一致），结果确定。"""
    hits = [_hit("c-b", "甲", 1.0), _hit("c-a", "乙", 2.0)]
    reranker = FastEmbedReranker(lambda query, documents: [0.5, 0.5])

    first = reranker.rerank("q", hits, top_k=2)
    second = reranker.rerank("q", hits, top_k=2)

    # 同分 → chunk_id 升序 [c-a, c-b]；且与传入顺序（c-b 在前）不同，
    # 证明平局规则生效而非保留传入序。
    assert [hit.chunk.chunk_id for hit in first] == ["c-a", "c-b"]
    assert [hit.chunk.chunk_id for hit in first] == [
        hit.chunk.chunk_id for hit in second
    ]


# ── 2. 边界与异常 ─────────────────────────────────────────────────


def test_rerank_empty_candidates_returns_empty_without_scoring() -> None:
    """空候选直接返回空，且不调用打分器（空列表是合法裁决）。"""
    calls: list[tuple[str, list[str]]] = []

    def scorer(query: str, documents: list[str]) -> list[float]:
        calls.append((query, documents))
        return []

    reranker = FastEmbedReranker(scorer)

    assert reranker.rerank("q", [], top_k=5) == []
    assert calls == []


def test_rerank_rejects_mismatched_score_count() -> None:
    """打分器返回的分数数与候选数不符 → ValueError（检索层据此降级）。"""
    reranker = FastEmbedReranker(lambda query, documents: [1.0])  # 候选 2 个

    with pytest.raises(ValueError, match="one score per candidate"):
        reranker.rerank("q", [_hit("c-a", "甲", 1.0), _hit("c-b", "乙", 1.0)], top_k=2)


def test_rerank_rejects_non_numeric_scores() -> None:
    """分数必须是数值：字符串 / bool / None 一律拒绝（bool 防御见模块注释）。"""
    for bad_score in ("1.0", True, None):

        def scorer(query: str, documents: list[str], value: Any = bad_score) -> list[Any]:
            return [value]

        reranker = FastEmbedReranker(scorer)
        with pytest.raises(TypeError, match="numeric"):
            reranker.rerank("q", [_hit("c-a", "甲", 1.0)], top_k=1)


def test_rerank_propagates_scorer_exception() -> None:
    """打分器异常原样上抛（检索层 _safe_rerank 负责降级，本层不吞错）。"""

    def broken(query: str, documents: list[str]) -> list[float]:
        raise RuntimeError("model unavailable")

    reranker = FastEmbedReranker(broken)

    with pytest.raises(RuntimeError, match="model unavailable"):
        reranker.rerank("q", [_hit("c-a", "甲", 1.0)], top_k=1)


def test_constructor_rejects_blank_model_name() -> None:
    """model_name 空白在构造期拒绝（尽早失败，不打分发请求才发现）。"""
    with pytest.raises(ValueError, match="model_name"):
        FastEmbedReranker(lambda query, documents: [], model_name="  ")


# ── 3. 真实打分函数装配（替身 encoder，零模型零网络）───────────────


class _FakeCrossEncoder:
    """fastembed TextCrossEncoder 替身：rerank 返回确定性的文档长度分。"""

    def __init__(self, model_name: str = "") -> None:
        self.model_name = model_name

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [float(len(document)) for document in documents]


def test_default_scorer_uses_loaded_cross_encoder_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认构造路径：经 _load_cross_encoder_class 拿到 encoder 类并实例化。

    测试替换加载入口为替身类（不联网下载模型）：rerank 结果由替身
    的「文档长度分」决定，证明默认 scorer 闭包真实调用 encoder.rerank。
    """
    monkeypatch.setattr(
        reranker_module, "_load_cross_encoder_class", lambda: _FakeCrossEncoder
    )
    reranker = FastEmbedReranker(model_name="fake-model")

    result = reranker.rerank(
        "q",
        [_hit("c-short", "甲", 1.0), _hit("c-long", "甲乙丙", 2.0)],
        top_k=2,
    )

    # 替身按文档长度打分：c-long（3 字）> c-short（1 字）→ 提到首位。
    assert [hit.chunk.chunk_id for hit in result] == ["c-long", "c-short"]
    assert [hit.score for hit in result] == [2.0, 1.0]  # 原分数保留


def test_default_scorer_raises_runtime_error_without_fastembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fastembed 未安装（加载入口抛 RuntimeError）→ 构造期上抛，由装配方降级。"""

    def _unavailable() -> object:
        raise RuntimeError("FastEmbedReranker 需要 fastembed 包")

    monkeypatch.setattr(reranker_module, "_load_cross_encoder_class", _unavailable)

    with pytest.raises(RuntimeError, match="fastembed"):
        FastEmbedReranker()


# ── 4. 与检索链路集成 ─────────────────────────────────────────────


def test_reranker_drives_final_order_through_service() -> None:
    """注入 KnowledgeService：最终顺序由重排决定，原词法分数透传。

    可手算场景（词法分数 = 命中查询词数）：query「支持向量机」与两个
    chunk 的交集相同 → 初检同分，平局按 chunk_id 升序 [c-reg, c-svm]；
    打分器只给含「间隔」的候选 1 分 → 重排把 c-svm 提到首位，顺序翻转
    为 [c-svm, c-reg]，而每个 hit 的 score 保持词法分数不改写
    （reranker.py 第 3 节「只改顺序不改分」契约）。
    """
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            KnowledgeChunk(
                chunk_id="c-svm",
                document_id="ml",
                content="支持向量机 间隔最大化",
                source="ml",
                page=None,
                start=0,
                end=12,
                metadata={},
            ),
            KnowledgeChunk(
                chunk_id="c-reg",
                document_id="ml",
                content="支持向量机 正则化",
                source="ml",
                page=None,
                start=0,
                end=9,
                metadata={},
            ),
        ]
    )
    baseline = KnowledgeService(index)
    reranked = KnowledgeService(
        index, reranker=FastEmbedReranker(_scorer_by_keyword("间隔"))
    )

    base_hits = baseline.search("支持向量机", top_k=5)
    new_hits = reranked.search("支持向量机", top_k=5)

    # 初检：同分平局按 chunk_id 升序 → [c-reg, c-svm]。
    assert [hit.chunk.chunk_id for hit in base_hits] == ["c-reg", "c-svm"]
    # 重排：含「间隔」的 c-svm 得 1 分提到首位 → 顺序翻转。
    assert [hit.chunk.chunk_id for hit in new_hits] == ["c-svm", "c-reg"]
    # 分数透传：重排后各 hit 的 score 与基线同一 chunk 的词法分数一致。
    base_scores = {hit.chunk.chunk_id: hit.score for hit in base_hits}
    for hit in new_hits:
        assert hit.score == base_scores[hit.chunk.chunk_id]
