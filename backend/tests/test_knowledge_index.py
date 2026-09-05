"""Tests for the replaceable knowledge index contract."""

from __future__ import annotations

from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "doc-1",
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=f"{document_id}.txt",
        page=None,
        start=0,
        end=len(content),
    )


def test_search_ranks_english_overlap_and_uses_chunk_id_for_ties() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("chunk-c", "Force creates acceleration."),
            _chunk("chunk-b", "Mass and force determine acceleration."),
            _chunk("chunk-a", "Mass and force determine acceleration."),
        ]
    )

    hits = index.search("FORCE mass", top_k=3)

    assert [hit.chunk.chunk_id for hit in hits] == ["chunk-a", "chunk-b", "chunk-c"]
    assert hits[0].score == hits[1].score > hits[2].score > 0
    assert hits[0].citation.chunk_id == "chunk-a"
    assert hits[0].citation.document_id == "doc-1"


def test_search_uses_chinese_bigrams_and_honors_top_k() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("mostly-related", "牛顿运动定律描述物体受力后的运动变化"),
            _chunk("partly-related", "牛顿研究了经典力学"),
            _chunk("unrelated", "化学反应需要满足守恒关系"),
        ]
    )

    hits = index.search("牛顿运动定律", top_k=1)

    assert [hit.chunk.chunk_id for hit in hits] == ["mostly-related"]
    assert hits[0].score > 0


def test_search_keeps_single_chinese_characters_searchable() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("mechanics", "力可以改变物体的运动状态")])

    hits = index.search("力", top_k=5)

    assert [hit.chunk.chunk_id for hit in hits] == ["mechanics"]


def test_upsert_replaces_an_existing_chunk_id() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert([_chunk("shared", "old vocabulary", document_id="old-doc")])
    replacement = _chunk("shared", "new concept", document_id="new-doc")

    index.upsert([replacement])

    assert index.search("old", top_k=5) == []
    hits = index.search("new", top_k=5)
    assert [hit.chunk for hit in hits] == [replacement]


def test_delete_document_removes_only_its_chunks() -> None:
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk("first", "shared term", document_id="doc-1"),
            _chunk("second", "shared term", document_id="doc-2"),
        ]
    )

    index.delete_document("doc-1")

    assert [hit.chunk.chunk_id for hit in index.search("shared", top_k=5)] == ["second"]


def test_search_returns_empty_for_empty_index_or_query() -> None:
    index = InMemoryKnowledgeIndex()

    assert index.search("anything", top_k=5) == []

    index.upsert([_chunk("chunk", "anything")])
    assert index.search("", top_k=5) == []
    assert index.search("   ", top_k=5) == []


# ── S5-C1 决策 3：namespace 读时归一与回填迁移 ─────────────────────


def test_inmemory_namespace_missing_key_normalizes_to_public() -> None:
    """读时归一：缺 namespace 键 ≡ "public"（正向与排除两个方向）。"""

    index = InMemoryKnowledgeIndex()
    legacy = _chunk("legacy", "线性代数与矩阵")
    explicit_x = KnowledgeChunk(
        chunk_id="course-x",
        document_id="course-x",
        content="线性代数与矩阵",
        source="course-x.txt",
        page=None,
        start=0,
        end=7,
        metadata={"namespace": "x"},
    )
    index.upsert([legacy, explicit_x])

    # 正向 public：缺键 chunk 命中，显式 x 不命中。
    public_hits = index.search("线性代数", top_k=5, metadata_filter={"namespace": "public"})
    assert [hit.chunk.chunk_id for hit in public_hits] == ["legacy"]
    # 正向 x：只命中显式 x。
    x_hits = index.search("线性代数", top_k=5, metadata_filter={"namespace": "x"})
    assert [hit.chunk.chunk_id for hit in x_hits] == ["course-x"]
    # 排除 !x：缺键 chunk 通过（public ≠ x），显式 x 被排除。
    not_x = index.search("线性代数", top_k=5, metadata_filter={"namespace": "!x"})
    assert [hit.chunk.chunk_id for hit in not_x] == ["legacy"]
    # 排除 !public：缺键 chunk（≡public）被排除，显式 x 通过。
    not_public = index.search("线性代数", top_k=5, metadata_filter={"namespace": "!public"})
    assert [hit.chunk.chunk_id for hit in not_public] == ["course-x"]


def _create_legacy_chunks_db(db_path) -> None:
    """手工构造「存量旧形态」词法库：chunks 表存在、行均无 namespace 键。"""
    import sqlite3

    raw = sqlite3.connect(db_path)
    raw.execute(
        """
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            page INTEGER,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """
    )
    raw.execute(
        "INSERT INTO chunks VALUES "
        "('c-legacy', 'd-legacy', '线性代数内容', 'd-legacy.txt', NULL, 0, 6, '{}')"
    )
    raw.commit()
    raw.close()


def test_sqlite_backfill_adds_public_namespace_on_open(tmp_path) -> None:
    """打开索引触发幂等回填：缺键行补 public；显式值保留；二次打开幂等。"""
    import json
    import sqlite3

    db = tmp_path / "legacy-knowledge.db"
    raw = sqlite3.connect(db)
    raw.execute(
        """
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            page INTEGER,
            start INTEGER NOT NULL,
            end INTEGER NOT NULL,
            metadata_json TEXT NOT NULL
        )
        """
    )
    raw.execute(
        "INSERT INTO chunks VALUES "
        "('c-legacy', 'd-legacy', '线性代数内容', 'd-legacy.txt', NULL, 0, 6, '{}')"
    )
    # 显式非 public 行：回填不得覆盖已有值。
    raw.execute(
        "INSERT INTO chunks VALUES "
        "('c-course', 'd-course', '课程讲义', 'd-course.txt', NULL, 0, 4, "
        '\'{"namespace": "x"}\')'
    )
    raw.commit()
    raw.close()

    first = SqliteKnowledgeIndex(db)
    rows = {
        chunk_id: json.loads(meta)
        for chunk_id, meta in first._conn.execute(
            "SELECT chunk_id, metadata_json FROM chunks"
        ).fetchall()
    }
    assert rows["c-legacy"]["namespace"] == "public"
    assert rows["c-course"]["namespace"] == "x"

    # 二次打开幂等：metadata_json 内容不变（json_set 对已含键行为零改写）。
    snapshot_before = dict(rows)
    SqliteKnowledgeIndex(db).close() if hasattr(SqliteKnowledgeIndex(db), "close") else None
    second = SqliteKnowledgeIndex(db)
    rows_after = {
        chunk_id: json.loads(meta)
        for chunk_id, meta in second._conn.execute(
            "SELECT chunk_id, metadata_json FROM chunks"
        ).fetchall()
    }
    assert rows_after == snapshot_before


def test_sqlite_read_time_normalization_covers_unbackfilled_rows(tmp_path) -> None:
    """读时归一防御层：open 之后外部灌入的缺键行同样 ≡ public。

    回填只在打开时执行；此后经另一连接直插的缺键行（模拟旧库拷贝/
    外部工具写库）由读时归一兜底。
    """
    import sqlite3

    db = tmp_path / "k.db"
    index = SqliteKnowledgeIndex(db)
    # 场景设定：无 FTS5 环境（_fts_enabled=False，回退全表扫描——与
    # C4 决策 4 的降级路径一致）。若 FTS 预筛启用，外部直插的行不在
    # FTS 表中、候选集必然缺失——那属于「写必须经索引 API」的契约外
    # 路径；读时归一防御层的适用面是回退模式。
    index._fts_enabled = False
    # 模拟外部灌库：绕过 upsert 直插缺键行。
    raw = sqlite3.connect(db)
    raw.execute(
        "INSERT INTO chunks VALUES "
        "('c-ext', 'd-ext', '外部灌入的线性代数内容', 'd-ext.txt', NULL, 0, 12, '{}')"
    )
    raw.commit()
    raw.close()

    hits = index.search("线性代数", top_k=5, metadata_filter={"namespace": "public"})
    assert [hit.chunk.chunk_id for hit in hits] == ["c-ext"]
