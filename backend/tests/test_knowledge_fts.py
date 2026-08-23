"""S5-C4 FTS5 候选预筛测试：与 InMemoryKnowledgeIndex 的语义等价性。

核心不变量：FTS 只圈定候选集合，打分/排序/平局/截断仍在 Python 侧
原样执行——同一语料上两条路径的搜索结果必须逐项一致。

覆盖清单：
1. 等价性矩阵：单字命中、相邻双字、英文词、无命中查询，在
   metadata_filter / ! 排除 / top_k 截断组合下逐项一致；
2. 回退规则：空 query → 空；词项超上限 → 回退全表扫描仍与 InMemory
   一致；FTS 禁用（漂移/探针失败）→ 同样一致；
3. 同步维护：upsert 覆盖 / delete_document 后候选集即时更新（无漂移）;
4. 行数漂移注入：告警 + 回退，检索结果不受影响；
5. 显式重建：rebuild_fts() 重建后重新启用。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import core.knowledge.index as index_module
from core.knowledge.index import (
    InMemoryKnowledgeIndex,
    KnowledgeChunk,
    SqliteKnowledgeIndex,
    _fts_match_expression,
    _fts_transform,
)

# 混合语料：覆盖 单字命中 / 相邻双字 / 英文词 / 无关内容 四类场景。
_CORPUS: list[tuple[str, str, dict[str, str]]] = [
    ("c1", "线性代数是机器学习的重要分支 convolutional networks", {"subject": "math"}),
    ("c2", "支持向量机与核方法", {"subject": "math"}),
    ("c3", "Deep learning optimizes neural networks", {"subject": "cs"}),
    ("c4", "完全无关的散文段落", {"subject": "cs"}),
    ("c5", "线性回归与梯度下降", {}),
]


def _chunks() -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for chunk_id, content, metadata in _CORPUS:
        chunks.append(
            KnowledgeChunk(
                chunk_id=chunk_id,
                document_id="doc",
                content=content,
                source=f"{chunk_id}.txt",
                page=None,
                start=0,
                end=len(content),
                metadata=metadata,
            )
        )
    return chunks


def _make_pair(tmp_path: Path) -> tuple[InMemoryKnowledgeIndex, SqliteKnowledgeIndex]:
    memory = InMemoryKnowledgeIndex()
    memory.upsert(_chunks())
    sqlite_index = SqliteKnowledgeIndex(tmp_path / "fts.db")
    sqlite_index.upsert(_chunks())
    return memory, sqlite_index


def _hit_ids(index: InMemoryKnowledgeIndex | SqliteKnowledgeIndex, *args: object, **kwargs: object) -> list[str]:
    hits = index.search(*args, **kwargs)  # type: ignore[arg-type]
    return [hit.chunk.chunk_id for hit in hits]


def test_fts_transform_splits_cjk_keeps_alnum() -> None:
    transformed = _fts_transform("机器学习 CNN")
    tokens = transformed.split()
    # CJK 逐字成独立 token；字母数字词保持完整（多空格对 FTS 无影响）。
    assert tokens == ["机", "器", "学", "习", "CNN"]


def test_fts_match_expression_shapes() -> None:
    # 单字 → 引号 token；bigram → 单引号串短语；混合 → OR 连接。
    expression = _fts_match_expression({"器", "线性", "cnn"})
    assert '"器"' in expression
    assert '"线 性"' in expression
    assert '"cnn"' in expression
    assert expression.count(" OR ") == 2


# ── 1. 等价性矩阵 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "top_k", "metadata_filter"),
    [
        ("线性", 5, None),  # 相邻双字 bigram
        ("器", 5, None),  # 单字（机器学习的器）
        ("convolutional", 5, None),  # 英文词
        ("深度学习", 5, None),  # 多字词（多 bigram 组合）
        ("如何学习线性代数", 5, None),  # 自然语言长查询
        ("不存在的词汇", 5, None),  # 无命中
        ("线性", 1, None),  # top_k 截断
        ("线性", 5, {"subject": "math"}),  # 正向过滤
        ("线性", 5, {"subject": "!math"}),  # 排除过滤
        ("学习", 2, {"subject": "!cs"}),
    ],
)
def test_fts_path_equivalent_to_inmemory(
    tmp_path: Path,
    query: str,
    top_k: int,
    metadata_filter: dict[str, str] | None,
) -> None:
    memory, sqlite_index = _make_pair(tmp_path)

    memory_hits = memory.search(query, top_k, metadata_filter=metadata_filter)
    fts_hits = sqlite_index.search(query, top_k, metadata_filter=metadata_filter)

    assert [(hit.chunk.chunk_id, hit.score) for hit in fts_hits] == [
        (hit.chunk.chunk_id, hit.score) for hit in memory_hits
    ]


def test_empty_query_returns_empty(tmp_path: Path) -> None:
    _, sqlite_index = _make_pair(tmp_path)
    assert sqlite_index.search("", 5) == []
    assert sqlite_index.search("   ", 5) == []


def test_over_limit_terms_fall_back_to_full_scan(tmp_path: Path) -> None:
    """词项数超过上限 → 回退全表扫描，结果与 InMemory 一致。"""
    memory, sqlite_index = _make_pair(tmp_path)
    # 构造 65 个互异 CJK 字的查询：前两个字命中 c1/c5 的「线 性」，其余
    # 为不存在字符——回退后全表扫描打分，结果应与 InMemory 完全一致。
    long_query = "".join(chr(0x4E00 + offset) for offset in range(65))

    # 回退路径（129 词项 > 64 上限）结果必须与 InMemory 逐项一致
    #（语料含区间内字符「与」，实际会命中部分 chunk——断言不预设具体值）。
    fallback_ids = _hit_ids(sqlite_index, long_query, 5)
    memory_ids = _hit_ids(memory, long_query, 5)
    assert fallback_ids == memory_ids


# ── 2. 同步维护 ────────────────────────────────────────────────────


def test_upsert_overkeep_and_delete_keep_fts_in_sync(tmp_path: Path) -> None:
    """覆盖 upsert 与 delete_document 后，候选集即时更新且无行数漂移。"""
    _, sqlite_index = _make_pair(tmp_path)

    # 覆盖 upsert：c4 内容换成含「梯度」的新文本。
    updated = KnowledgeChunk(
        chunk_id="c4",
        document_id="doc",
        content="梯度下降是优化算法",
        source="c4.txt",
        page=None,
        start=0,
        end=9,
        metadata={},
    )
    sqlite_index.upsert([updated])
    assert _hit_ids(sqlite_index, "梯度", 5) == ["c4", "c5"]

    # 删除文档：候选集同步收缩。
    sqlite_index.delete_document("doc")
    conn = sqlite3.connect(tmp_path / "fts.db")
    try:
        fts_count = conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
        chunks_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        conn.close()
    assert fts_count == chunks_count == 0


# ── 3. 行数漂移与禁用回退 ──────────────────────────────────────────


def test_row_drift_disables_fts_but_results_stay_correct(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """行数漂移 → 告警 + 回退全表扫描，检索结果不受影响。"""
    db_path = tmp_path / "drift.db"
    # 直接构造：先建库再注入漂移（删掉一半 FTS 行）。
    memory = InMemoryKnowledgeIndex()
    memory.upsert(_chunks())
    sqlite_index = SqliteKnowledgeIndex(db_path)
    sqlite_index.upsert(_chunks())

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DELETE FROM chunks_fts WHERE chunk_id IN ('c1','c2')")
        raw.commit()
    finally:
        raw.close()

    with caplog.at_level("WARNING", logger=index_module._LOGGER.name):
        reopened = SqliteKnowledgeIndex(db_path)

    assert reopened._fts_enabled is False
    assert any("行数漂移" in message for message in caplog.messages)
    # 回退路径结果仍正确。
    assert _hit_ids(reopened, "线性", 5) == ["c1", "c5"]


def test_probe_failure_falls_back_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境无 FTS5（探针失败）→ 告警回退，检索走全表扫描。"""
    monkeypatch.setattr(index_module, "_fts5_supported", lambda conn: False)
    db_path = tmp_path / "nofs.db"

    index = SqliteKnowledgeIndex(db_path)
    index.upsert(_chunks())

    assert index._fts_enabled is False
    # 回退全表扫描：检索结果与 FTS 路径一致（优雅降级的意义所在）。
    memory = InMemoryKnowledgeIndex()
    memory.upsert(_chunks())
    assert _hit_ids(index, "线性", 5) == _hit_ids(memory, "线性", 5)


# ── 4. 显式重建 ────────────────────────────────────────────────────


def test_rebuild_fts_restores_prefilter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """漂移禁用后 rebuild_fts 重建并重新启用预筛。"""
    db_path = tmp_path / "rebuild.db"
    memory = InMemoryKnowledgeIndex()
    memory.upsert(_chunks())
    index = SqliteKnowledgeIndex(db_path)
    index.upsert(_chunks())

    # 注入漂移使预筛禁用。
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DELETE FROM chunks_fts WHERE chunk_id = 'c1'")
        raw.commit()
    finally:
        raw.close()
    reopened = SqliteKnowledgeIndex(db_path)
    assert reopened._fts_enabled is False

    rebuilt_rows = reopened.rebuild_fts()

    assert rebuilt_rows == len(_CORPUS)
    assert reopened._fts_enabled is True
    assert _hit_ids(reopened, "线性", 5) == _hit_ids(memory, "线性", 5)
