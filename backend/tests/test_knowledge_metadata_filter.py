"""S3-T3 领域元数据与过滤检索测试。

覆盖清单 A S3-T3 验收标准：
1. 字段写入：chunk metadata 含清单注入字段（subject/difficulty/title）
   与规则提取字段（chapter/section/tags），字符/语义两种分块下
   章节提取都可用；
2. 过滤检索：单条件（source/难度/章节/小节/tags）、组合条件（AND）、
   过滤后空结果返回空列表不报错、过滤先于排序与 top_k 截断；
3. InMemory 与 SQLite 两实现过滤语义一致（参数化锁定）；
4. ingest 端到端：清单注入字段与规则提取字段合流写入 chunk metadata。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ingest_books import ManifestBook, ingest_book

from core.knowledge.chunking import chunk_document, chunk_document_semantic
from core.knowledge.index import (
    InMemoryKnowledgeIndex,
    KnowledgeIndex,
    SqliteKnowledgeIndex,
)
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument
from core.knowledge.service import KnowledgeService

# ── 小工具 ────────────────────────────────────────────────────────


def _chunk(
    chunk_id: str,
    content: str,
    *,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> KnowledgeChunk:
    """构造一个可直接入库的 chunk（metadata 由测试显式指定）。"""
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        content=content,
        source=source,
        page=None,
        start=0,
        end=len(content),
        metadata=metadata or {},
    )


def _domain_chunks() -> list[KnowledgeChunk]:
    """领域元数据样本：3 个 chunk，覆盖不同书/难度/章节/标签。

    query "support vector machine" 的词法分数：
    - c1: 3 分（含全部三词），difficulty=advanced（多数过滤用例会排除它）；
    - c2: 3 分，difficulty=intermediate；
    - c3: 2 分，difficulty=intermediate。
    这样「过滤先于排序截断」的可区分度最强：不过滤时 top_k=1 是 c1，
    过滤 intermediate 后 top_k=1 是 c2。
    """
    return [
        _chunk(
            "c1",
            "support vector machine kernel margin",
            source="ml-zhouzhihua",
            metadata={
                "subject": "机器学习",
                "difficulty": "advanced",
                "chapter": "第6章",
                "section": "6.1",
                "tags": ["支持向量机", "核函数"],
            },
        ),
        _chunk(
            "c2",
            "support vector machine",
            source="ml-zhouzhihua",
            metadata={
                "subject": "机器学习",
                "difficulty": "intermediate",
                "chapter": "第6章",
                "section": "6.2",
                "tags": ["支持向量机"],
            },
        ),
        _chunk(
            "c3",
            "support vector",
            source="dl-d2l",
            metadata={
                "subject": "深度学习",
                "difficulty": "intermediate",
                "chapter": "第3章",
                "tags": ["注意力机制"],
            },
        ),
    ]


@pytest.fixture(params=["memory", "sqlite"])
def index(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[KnowledgeIndex]:
    """同一套用例跑 InMemory 与 SQLite 两实现，锁定过滤语义一致。"""
    if request.param == "memory":
        yield InMemoryKnowledgeIndex()
        return
    instance = SqliteKnowledgeIndex(tmp_path / "kb.db")
    yield instance
    instance.close()


# ── 1. 章节字段规则提取（chunking 层）────────────────────────────


def test_heading_variants_yield_chapter_section_and_tags() -> None:
    """三种标题形态都能提取出章节层级字段与概念标签。"""
    cases = [
        ("第 1 章 支持向量机\n正文", {"chapter": "第1章", "tags": ["支持向量机"]}),
        ("第三章 监督学习\n正文", {"chapter": "第三章", "tags": ["监督学习"]}),
        ("3.2.1 核函数\n正文", {"section": "3.2.1", "tags": ["核函数"]}),
        ("## 注意力机制\n正文", {"tags": ["注意力机制"]}),
    ]
    for content, expected in cases:
        chunks = chunk_document_semantic(
            KnowledgeDocument(document_id="doc", content=content, source="doc.txt"),
            max_chunk_size=1000,
        )
        metadata = chunks[0].metadata
        for key, value in expected.items():
            assert metadata[key] == value, f"{content!r}: 缺 {key}={value!r}"


def test_semantic_chunks_carry_their_own_heading_metadata() -> None:
    """语义分块：标题开启新 chunk，chunk 的 chapter/tags 精确取自该标题。"""
    content = (
        "第 1 章 支持向量机\n\n支持向量机 间隔 核函数\n\n"
        "第 2 章 条件随机场\n\n概率 标注"
    )
    chunks = chunk_document_semantic(
        KnowledgeDocument(document_id="doc", content=content, source="doc.txt"),
        max_chunk_size=1000,
    )

    assert len(chunks) == 2
    assert chunks[0].metadata["chapter"] == "第1章"
    assert "支持向量机" in chunks[0].metadata["tags"]
    assert chunks[1].metadata["chapter"] == "第2章"
    assert "条件随机场" in chunks[1].metadata["tags"]


def test_character_chunks_inherit_nearest_heading() -> None:
    """字符分块：窗口不包含标题也能按「起点之前最近标题」标注所属章节。

    文档结构（偏移均为左闭右开）：前言「前言段落。」= 0-4；空行 = 5-6；
    标题行「第 1 章 支持向量机」= 7-17；空行 = 18-19；正文 = 20-32。
    窗口 1 = [0, 18)（起点 0 在标题之前）→ 无章节标注；窗口 2 =
    [18, 33)（起点紧接标题行之后）→ 继承第 1 章——证明章节提取
    对字符分块同样可用。
    """
    content = "前言段落。\n\n第 1 章 支持向量机\n\n支持向量机 间隔 核函数。"
    chunks = chunk_document(
        KnowledgeDocument(document_id="doc", content=content, source="doc.txt"),
        chunk_size=18,
        overlap=0,
    )

    assert len(chunks) == 2
    assert "chapter" not in chunks[0].metadata
    assert chunks[1].metadata["chapter"] == "第1章"
    assert "支持向量机" in chunks[1].metadata["tags"]


def test_document_without_headings_keeps_metadata_unchanged() -> None:
    """无标题文档：不写章节字段，既有 metadata 行为不变。"""
    document = KnowledgeDocument(
        document_id="doc",
        content="正文内容。",
        source="doc.txt",
        metadata={"subject": "ml"},
    )

    semantic_chunks = chunk_document_semantic(document)
    character_chunks = chunk_document(document, chunk_size=10, overlap=0)

    assert semantic_chunks[0].metadata == {"subject": "ml", "chunking": "semantic"}
    assert character_chunks[0].metadata == {"subject": "ml"}


# ── 2. 过滤检索（index 层，两实现参数化）─────────────────────────


def test_filter_by_source(index: KnowledgeIndex) -> None:
    """单条件：限定某本书（source 键映射顶层字段）。"""
    index.upsert(_domain_chunks())

    hits = index.search(
        "support vector machine", top_k=10, metadata_filter={"source": "dl-d2l"}
    )

    assert [hit.chunk.chunk_id for hit in hits] == ["c3"]


def test_filter_by_difficulty(index: KnowledgeIndex) -> None:
    """单条件：按难度过滤。"""
    index.upsert(_domain_chunks())

    hits = index.search(
        "support vector machine",
        top_k=10,
        metadata_filter={"difficulty": "intermediate"},
    )

    assert [hit.chunk.chunk_id for hit in hits] == ["c2", "c3"]


def test_filter_by_chapter_and_section(index: KnowledgeIndex) -> None:
    """单条件：按章节（chapter）与按小节（section）过滤。"""
    index.upsert(_domain_chunks())

    chapter_hits = index.search(
        "support vector machine", top_k=10, metadata_filter={"chapter": "第6章"}
    )
    section_hits = index.search(
        "support vector machine", top_k=10, metadata_filter={"section": "6.2"}
    )

    assert [hit.chunk.chunk_id for hit in chapter_hits] == ["c1", "c2"]
    assert [hit.chunk.chunk_id for hit in section_hits] == ["c2"]


def test_filter_by_tags_list_value(index: KnowledgeIndex) -> None:
    """概念标签是字符串列表：任一元素相等即匹配。"""
    index.upsert(_domain_chunks())

    hits = index.search(
        "support vector machine", top_k=10, metadata_filter={"tags": "核函数"}
    )

    assert [hit.chunk.chunk_id for hit in hits] == ["c1"]


def test_filter_combines_conditions_with_and(index: KnowledgeIndex) -> None:
    """组合条件：多键是 AND 关系（学科 + 难度 / 书 + 章节）。"""
    index.upsert(_domain_chunks())

    combined = index.search(
        "support vector machine",
        top_k=10,
        metadata_filter={"subject": "机器学习", "difficulty": "intermediate"},
    )
    source_chapter = index.search(
        "support vector machine",
        top_k=10,
        metadata_filter={"source": "ml-zhouzhihua", "chapter": "第6章"},
    )

    # c1 是机器学习但难度 advanced，c3 难度 intermediate 但学科不同 → 只剩 c2。
    assert [hit.chunk.chunk_id for hit in combined] == ["c2"]
    assert [hit.chunk.chunk_id for hit in source_chapter] == ["c1", "c2"]


def test_filter_empty_result_returns_empty_list(index: KnowledgeIndex) -> None:
    """过滤后无匹配：返回空列表，不抛错。"""
    index.upsert(_domain_chunks())

    no_difficulty = index.search(
        "support vector machine", top_k=10, metadata_filter={"difficulty": "beginner"}
    )
    no_source = index.search(
        "support vector machine", top_k=10, metadata_filter={"source": "ghost-book"}
    )
    missing_key = index.search(
        "support vector machine", top_k=10, metadata_filter={"chapter": "第9章"}
    )
    # 组合条件也无匹配：c1 机器学习但 advanced、c2 机器学习 intermediate、
    # c3 深度学习 intermediate → 没有任何 chunk 同时满足两个条件。
    no_combination = index.search(
        "support vector machine",
        top_k=10,
        metadata_filter={"subject": "机器学习", "difficulty": "beginner"},
    )

    assert no_difficulty == []
    assert no_source == []
    assert missing_key == []
    assert no_combination == []


def test_filter_applies_before_top_k_truncation(index: KnowledgeIndex) -> None:
    """过滤先于排序与 top_k 截断：全局最高分的 chunk 被过滤条件排除。

    独立数据集（中文 query 产生更多词素，分数区分度更高）：
    - high: 14 分（"支持向量机 核函数"全部词素命中），advanced；
    - mid: 9 分，intermediate；
    - low: 5 分，intermediate。
    """
    index.upsert(
        [
            _chunk(
                "high",
                "支持向量机 核函数 间隔 分类 回归",
                source="ml-zhouzhihua",
                metadata={"subject": "机器学习", "difficulty": "advanced"},
            ),
            _chunk(
                "mid",
                "支持向量机",
                source="ml-zhouzhihua",
                metadata={"subject": "机器学习", "difficulty": "intermediate"},
            ),
            _chunk(
                "low",
                "核函数",
                source="dl-d2l",
                metadata={"subject": "深度学习", "difficulty": "intermediate"},
            ),
        ]
    )

    unfiltered = index.search("支持向量机 核函数", top_k=1)
    filtered = index.search(
        "支持向量机 核函数",
        top_k=1,
        metadata_filter={"difficulty": "intermediate"},
    )

    # 不过滤时第一名是 high（14 分，全局最高）；过滤 intermediate 后
    # high 被排除，第一名变为 mid（9 分 > low 5 分）。若实现是
    # 「先截断 top_k=1 再过滤」，结果会是空列表——本断言锁定
    # 「先过滤、后排序、最后截断」的顺序。
    assert [hit.chunk.chunk_id for hit in unfiltered] == ["high"]
    assert [hit.chunk.chunk_id for hit in filtered] == ["mid"]
    assert filtered[0].score < unfiltered[0].score


def test_filter_returns_all_matches_when_fewer_than_top_k(
    index: KnowledgeIndex,
) -> None:
    """截断发生在过滤之后：过滤后匹配数少于 top_k 时返回全部匹配。"""
    index.upsert(_domain_chunks())

    hits = index.search(
        "support vector machine",
        top_k=5,
        metadata_filter={"difficulty": "intermediate"},
    )

    assert [hit.chunk.chunk_id for hit in hits] == ["c2", "c3"]


def test_no_filter_preserves_existing_ranking(index: KnowledgeIndex) -> None:
    """不传过滤条件时行为与 S3-T1/T2 完全一致（分数排序 + chunk_id 平局）。"""
    index.upsert(_domain_chunks())

    hits = index.search("support vector machine", top_k=10)

    assert [hit.chunk.chunk_id for hit in hits] == ["c1", "c2", "c3"]
    assert hits[0].score == hits[1].score > hits[2].score


def test_filter_empty_dict_is_noop(index: KnowledgeIndex) -> None:
    """空 dict 过滤条件等价于不过滤（结果与不传 metadata_filter 一致）。"""
    index.upsert(_domain_chunks())

    plain = index.search("support vector machine", top_k=10)
    empty_filter = index.search(
        "support vector machine", top_k=10, metadata_filter={}
    )

    assert [hit.chunk.chunk_id for hit in empty_filter] == [
        hit.chunk.chunk_id for hit in plain
    ]


def test_filter_rejects_invalid_key(index: KnowledgeIndex) -> None:
    """键名非法（格式错误，防 JSON path 注入）→ ValueError。"""
    index.upsert(_domain_chunks())

    with pytest.raises(ValueError, match="metadata_filter"):
        index.search(
            "support vector machine",
            top_k=10,
            metadata_filter={"subject!": "机器学习"},  # type: ignore[dict-item]
        )


def test_filter_rejects_non_string_value(index: KnowledgeIndex) -> None:
    """过滤值类型错误（非字符串）→ TypeError（ruff TRY004 语义）。"""
    index.upsert(_domain_chunks())

    with pytest.raises(TypeError, match="metadata_filter"):
        index.search(  # type: ignore[arg-type]
            "support vector machine",
            top_k=10,
            metadata_filter={"subject": 123},
        )
    with pytest.raises(TypeError, match="metadata_filter"):
        index.search(  # type: ignore[arg-type]
            "support vector machine",
            top_k=10,
            metadata_filter={"subject": None},
        )
    # 整个过滤条件不是 dict 也是类型错误。
    with pytest.raises(TypeError, match="metadata_filter"):
        index.search(  # type: ignore[arg-type]
            "support vector machine", top_k=10, metadata_filter="subject"
        )


def test_chapter_filter_works_on_chunking_output(index: KnowledgeIndex) -> None:
    """端到端：语义分块产物入库后可按章节过滤（字符分块同样可用）。

    两种分块策略产出的 chunk 都带 chapter 字段（提取规则见
    test_*_chunks_* 用例），因此过滤链路对两者一致生效。
    """
    content = (
        "第 1 章 支持向量机\n\n支持向量机 间隔 核函数\n\n"
        "第 2 章 条件随机场\n\n概率 标注"
    )
    document = KnowledgeDocument(document_id="book", content=content, source="book.txt")
    semantic_chunks = chunk_document_semantic(document, max_chunk_size=1000)
    character_chunks = chunk_document(document, chunk_size=100, overlap=0)

    for chunks in (semantic_chunks, character_chunks):
        index.upsert(chunks)
        hits = index.search(
            "支持向量机", top_k=10, metadata_filter={"chapter": "第1章"}
        )
        assert hits
        assert all(hit.chunk.metadata.get("chapter") == "第1章" for hit in hits)
        index.delete_document("book")


# ── 3. service 层透传 ─────────────────────────────────────────────


def test_service_search_accepts_metadata_filter() -> None:
    """KnowledgeService.search 透传过滤条件：命中过滤、空结果不报错。"""
    service = KnowledgeService(InMemoryKnowledgeIndex())
    service.add_documents(
        [
            KnowledgeDocument(
                document_id="ml",
                content="support vector machine kernel",
                source="ml-zhouzhihua",
                metadata={"subject": "机器学习", "difficulty": "intermediate"},
            ),
            KnowledgeDocument(
                document_id="dl",
                content="support vector",
                source="dl-d2l",
                metadata={"subject": "深度学习", "difficulty": "beginner"},
            ),
        ]
    )

    hits = service.search(
        "support vector", top_k=5, metadata_filter={"subject": "机器学习"}
    )
    empty = service.search(
        "support vector", top_k=5, metadata_filter={"difficulty": "advanced"}
    )

    assert [hit.chunk.document_id for hit in hits] == ["ml"]
    assert empty == []


# ── 4. ingest 端到端：清单注入 + 规则提取合流 ────────────────────


def test_ingest_combines_injected_and_extracted_metadata(tmp_path: Path) -> None:
    """入库后 chunk metadata 同时含清单注入字段与规则提取字段。

    - 清单注入：subject/difficulty/title（来自 ManifestBook）；
    - 规则提取：chapter/tags（chunking 从标题行解析）。
    """
    index = SqliteKnowledgeIndex(tmp_path / "kb.db")
    try:
        book = ManifestBook(
            source="ml-a",
            file="book.pdf",
            title="《ml-a》",
            authors=["测试作者"],
            subjects=["机器学习"],
            difficulty="intermediate",
            blocked=None,
        )

        def headed_loader(
            path: Path, document_id: str, source_label: str
        ) -> Iterator[KnowledgeDocument]:
            yield KnowledgeDocument(
                document_id=document_id,
                content="第 1 章 支持向量机\n\n支持向量机 间隔 核函数",
                source=source_label,
                page=1,
            )

        result = ingest_book(
            index,
            book,
            Path("book.pdf"),
            page_loader=headed_loader,
            chunking="semantic",
        )
        assert result.status == "ingested"

        hits = KnowledgeService(index).search(
            "支持向量机", top_k=5, metadata_filter={"chapter": "第1章"}
        )
        assert hits
        metadata = hits[0].chunk.metadata
        assert metadata["subject"] == "机器学习"
        assert metadata["difficulty"] == "intermediate"
        assert metadata["title"] == "《ml-a》"
        assert metadata["chapter"] == "第1章"
        assert "支持向量机" in metadata["tags"]
    finally:
        index.close()
