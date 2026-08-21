"""S5 检索评测脚本（evaluate_retrieval.py）纯函数与评测集装配测试。

覆盖：指标函数（rank / Recall@K / MRR）的正常与边界路径、评测集
装配（manifest verify 用例 + 内置扩展合并、blocked 书目跳过、按
query 去重、空集防御）。检索执行与报告渲染走真实库，不在单测范围
（验证计划由真实库评测运行承担）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evaluate_retrieval import (
    load_eval_cases,
    mean_reciprocal_rank,
    rank_of_expected,
    recall_at_k,
)

from core.knowledge.models import Citation, KnowledgeChunk, SearchHit


def _hit(chunk_id: str, source: str) -> SearchHit:
    chunk = KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=source,
        content="正文",
        source=source,
        page=None,
        start=0,
        end=2,
        metadata={},
    )
    return SearchHit(
        chunk=chunk,
        citation=Citation(
            document_id=source, source=source, page=None, chunk_id=chunk_id
        ),
        score=1.0,
    )


def test_rank_of_expected_returns_first_matching_rank() -> None:
    """首个期望 source 命中的排名（1 起）；同名文档多条命中取最前。"""
    hits = [_hit("c1", "a"), _hit("c2", "b"), _hit("c3", "b")]
    assert rank_of_expected(hits, "b") == 2
    assert rank_of_expected(hits, "a") == 1


def test_rank_of_expected_returns_none_when_missing() -> None:
    """未命中期望 source → None（Recall/MRR 据此计 0）。"""
    assert rank_of_expected([_hit("c1", "a")], "b") is None
    assert rank_of_expected([], "b") is None


def test_recall_at_k_counts_hits_within_k() -> None:
    ranks = [1, 3, None, 5]
    assert recall_at_k(ranks, 1) == pytest.approx(1 / 4)
    assert recall_at_k(ranks, 5) == pytest.approx(3 / 4)
    assert recall_at_k(ranks, 3) == pytest.approx(2 / 4)


def test_recall_at_k_empty_ranks_returns_zero() -> None:
    assert recall_at_k([], 5) == 0.0


def test_mean_reciprocal_rank() -> None:
    # rank 1 → 1.0，rank 2 → 0.5，None → 0；均值 = (1 + 0.5 + 0) / 3
    assert mean_reciprocal_rank([1, 2, None]) == pytest.approx((1 + 0.5 + 0) / 3)
    assert mean_reciprocal_rank([]) == 0.0


def _write_manifest(path: Path, books: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"version": 1, "books": books}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_load_eval_cases_merges_manifest_and_extended(tmp_path: Path) -> None:
    """清单用例与内置扩展合并；blocked 书目跳过；query 去重（清单优先）。"""
    manifest = tmp_path / "manifest.json"
    # 内置扩展里已含「过拟合怎么缓解」→ ml-zhouzhihua；清单放同 query
    # 指向另一本书，验证去重以清单为准（先出现者保留）。
    _write_manifest(
        manifest,
        [
            {
                "source": "book-a",
                "verify": [{"query": "过拟合怎么缓解", "expected_source": "book-a"}],
            },
            {
                "source": "book-b",
                "blocked": "scanned-pdf-no-text-layer",
                "verify": [{"query": "不可达用例", "expected_source": "book-b"}],
            },
        ],
    )

    cases = load_eval_cases(manifest)

    by_query = {case.query: case for case in cases}
    # 清单用例保留且优先于扩展里的同 query 用例。
    assert by_query["过拟合怎么缓解"].expected_source == "book-a"
    assert by_query["过拟合怎么缓解"].origin == "manifest"
    # blocked 书目的用例被跳过。
    assert "不可达用例" not in by_query
    # 内置扩展用例合入（扩展集非空时总数 > 清单条目数）。
    assert any(case.origin == "extended" for case in cases)
    assert len(cases) > 1


def test_load_eval_cases_rejects_empty_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单无任何可用用例且扩展集为空时抛错（防御：不产出空报告）。"""
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, [{"source": "book-a", "blocked": "x", "verify": []}])

    # 扩展集非空时仍可评测——用 monkeypatch 清空扩展集模拟「真空」。
    import evaluate_retrieval

    monkeypatch.setattr(evaluate_retrieval, "_EXTENDED_CASES", [])
    with pytest.raises(ValueError, match="评测集为空"):
        load_eval_cases(manifest)


def test_load_eval_cases_rejects_unreadable_manifest(tmp_path: Path) -> None:
    """清单缺失/损坏 → ValueError（尽早失败，不产出误导性报告）。"""
    with pytest.raises(ValueError, match="无法读取知识清单"):
        load_eval_cases(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="无法读取知识清单"):
        load_eval_cases(broken)
