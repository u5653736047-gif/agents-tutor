"""S3-T2 语义分块测试。

覆盖清单 A S3-T2 验收标准：
1. 章节标题 / 段落边界切分（标题识别、段落组装、坐标正确）；
2. 公式段与代码块最小保护（$$ 长公式、跨段公式、围栏代码块、
   缩进代码块不被从中间截断）；
3. 坐标一致性（每 chunk 坐标可定位回原文、chunk_id 由坐标派生、
   chunk 区间不重叠）；
4. 策略参数选择生效（semantic 与 character 产出不同 chunk 集、
   character 默认行为不变、非法策略被拒绝）。
"""

from __future__ import annotations

import pytest

from core.knowledge.chunking import (
    chunk_document_semantic,
    chunk_documents,
    chunk_documents_semantic,
)
from core.knowledge.index import InMemoryKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument
from core.knowledge.service import KnowledgeService


def _document(content: str, **overrides: object) -> KnowledgeDocument:
    """构造测试文档：默认 document_id="doc"、source="doc.txt"。"""
    return KnowledgeDocument(
        document_id="doc", content=content, source="doc.txt", **overrides
    )


def _assert_coordinates(
    chunks: list[KnowledgeChunk], document: KnowledgeDocument
) -> None:
    """坐标一致性断言：内容可回指原文、区间单调不重叠、chunk_id 由坐标派生。"""
    previous_end = 0
    for chunk in chunks:
        assert 0 <= chunk.start < chunk.end <= len(document.content)
        assert chunk.content == document.content[chunk.start : chunk.end]
        assert chunk.start >= previous_end, "chunk 区间不得重叠"
        previous_end = chunk.end
        page = chunk.page if chunk.page is not None else 0
        assert chunk.chunk_id == (
            f"{chunk.document_id}:{page}:{chunk.start}:{chunk.end}"
        )


def _covering_chunks(
    chunks: list[KnowledgeChunk], document: KnowledgeDocument, fragment: str
) -> list[KnowledgeChunk]:
    """返回完整包含指定原文片段的 chunk（按坐标定位，验证可回溯性）。

    完整包含 = chunk 区间覆盖片段的整个 [offset, offset+len) 区间，
    而不是只覆盖片段起点——这样「不被截断」的断言才名副其实。
    """
    offset = document.content.index(fragment)
    return [
        chunk
        for chunk in chunks
        if chunk.start <= offset and chunk.end >= offset + len(fragment)
    ]


# ── 章节标题 / 段落边界切分 ───────────────────────────────────────


def test_semantic_splits_at_heading_boundaries() -> None:
    """章节标题切分：每章一个 chunk，标题行完整保留在 chunk 开头。"""
    content = (
        "第 1 章 引言\n"
        "本书介绍机器学习的基本概念。\n"
        "\n"
        "第 2 章 监督学习\n"
        "监督学习是本书的核心内容。\n"
        "\n"
        "第 3 章 无监督学习\n"
        "无监督学习处理未标注数据。\n"
    )
    document = _document(content)
    chunks = chunk_document_semantic(document, max_chunk_size=1000)

    assert len(chunks) == 3
    assert chunks[0].content.startswith("第 1 章")
    assert chunks[1].content.startswith("第 2 章")
    assert chunks[2].content.startswith("第 3 章")
    assert "第 2 章" not in chunks[0].content  # 章边界切分干净，不串章
    _assert_coordinates(chunks, document)


@pytest.mark.parametrize(
    "heading",
    [
        "## 支持向量机\n正文内容。",
        "第三章 监督学习\n正文内容。",
        "3.2.1 核函数\n正文内容。",
    ],
)
def test_semantic_heading_variants(heading: str) -> None:
    """不同标题形态（Markdown / 中文章节 / 数字小节）都能识别并切分。"""
    content = "前置段落。\n\n" + heading + "\n\n结尾段落。"
    document = _document(content)
    chunks = chunk_document_semantic(document, max_chunk_size=1000)

    assert len(chunks) == 2
    assert chunks[1].content.startswith(heading.splitlines()[0])
    _assert_coordinates(chunks, document)


def test_semantic_merges_paragraphs_up_to_max_size() -> None:
    """无标题时按段落组装：并入后超过 max_chunk_size 才在段落边界切分。"""
    paragraph = "段落内容。" + "填充" * 300  # 约 605 字符
    content = f"{paragraph}\n\n{paragraph}\n\n{paragraph}"
    document = _document(content)
    chunks = chunk_document_semantic(document, max_chunk_size=1000)

    # 每段约 605 字符：两段合并约 1212 > 1000 → 每段独立成 chunk。
    assert len(chunks) == 3
    # 段间空行归属前一个 chunk（坐标切片语义，见 _paragraph_spans），
    # 去掉尾部换行后每块内容恰为一段。
    assert all(chunk.content.rstrip("\n") == paragraph for chunk in chunks)
    _assert_coordinates(chunks, document)


def test_semantic_oversized_paragraph_splits_at_line_boundaries() -> None:
    """超长段落内部切分：优先切在行尾而不是句子中间。"""
    lines = ["第" + "i" * 150 for _ in range(10)]  # 每行 151 字符
    content = "\n".join(lines)
    document = _document(content)
    chunks = chunk_document_semantic(
        document, max_chunk_size=400, min_chunk_size=150
    )

    _assert_coordinates(chunks, document)
    assert len(chunks) > 1
    # 切点都在行边界（行尾换行符归属前一个 chunk）：每个 chunk 的
    # 起点是行首、终点是行尾，任何行都不会被从中间截断。
    for chunk in chunks:
        assert chunk.start == 0 or document.content[chunk.start - 1] == "\n"
        assert (
            chunk.end == len(document.content)
            or document.content[chunk.end - 1] == "\n"
        )


# ── 公式段与代码块最小保护 ────────────────────────────────────────


def test_semantic_keeps_long_latex_formula_whole() -> None:
    """长 $$ 公式完整保留：窗口切点落到公式中间时被推到公式结束。"""
    formula = "$$" + "x_{" + "i" * 3000 + "}" + "$$"
    content = "前置段落说明。\n\n" + formula + "\n\n后置段落说明。"
    document = _document(content)
    chunks = chunk_document_semantic(document, max_chunk_size=500)

    covering = _covering_chunks(chunks, document, formula)
    assert covering, "公式必须整体落在某个 chunk 内，不得被截断"
    _assert_coordinates(chunks, document)


def test_semantic_version_number_is_treated_as_heading() -> None:
    """数字小节启发式对版本号行的取舍：接受误判并锁定当前行为。

    如 "2024.5.1 版本说明" 会命中数字小节模式而被当作标题开启新 chunk
    ——这是有意接受的启发式误判（见 chunking._HEADING_RE 注释）：后果
    只是分块粒度变细，坐标与可回溯性不受影响；若加「版本/年版」等中文
    字面特判来排除版本号，反而会误伤 "3.2.1 版本管理" 这类真实小节标题。
    """
    content = "正文段落。\n\n2024.5.1 版本说明\n版本记录内容。\n\n结尾段落。"
    document = _document(content)
    chunks = chunk_document_semantic(document, max_chunk_size=1000)

    # 锁定当前行为：版本号行被当作标题，开启一个新 chunk（粒度变细）。
    assert len(chunks) == 2
    assert chunks[1].content.startswith("2024.5.1")
    _assert_coordinates(chunks, document)


def test_semantic_protected_block_crossing_paragraphs_stays_whole() -> None:
    """保护块跨段（公式内部有空行）：段落边界不生效，公式整体保留。"""
    formula = "$$" + "长" * 600 + "\n\n" + "续" * 600 + "$$"
    content = "前置段落。\n\n" + formula + "\n\n后置段落。"
    document = _document(content)
    # max_chunk_size=200 时，若 min_chunk_size 取默认值 200 会触发
    # 「min 必须严格小于 max」的校验失败，因此显式传一个更小的值。
    # 说明：本用例公式整体跨段（内部含空行），但按空行切出的每个公式
    # 段内部没有换行符，因此 min 的行边界取舍不参与切点计算，取值只
    # 影响校验通过与否，不影响分块结果。
    chunks = chunk_document_semantic(
        document, max_chunk_size=200, min_chunk_size=50
    )

    covering = _covering_chunks(chunks, document, formula)
    assert covering, "跨段公式必须整体保留在某个 chunk 内"
    _assert_coordinates(chunks, document)


def test_semantic_keeps_code_fence_whole() -> None:
    """围栏代码块完整保留：切分点不得落在 ``` 块中间。"""
    code = "```python\n" + "print('hello')\n" * 300 + "```"
    content = "代码示例：\n\n" + code + "\n\n后续正文。"
    document = _document(content)
    chunks = chunk_document_semantic(document, max_chunk_size=300)

    covering = _covering_chunks(chunks, document, code)
    assert covering, "围栏代码块必须整体保留在某个 chunk 内"
    _assert_coordinates(chunks, document)


def test_semantic_keeps_indented_code_block_whole() -> None:
    """缩进代码块（4 空格开头）完整保留：窗口切点落到块内时被推后。"""
    # 保护块结束在最后一行行尾（不含行尾换行符），因此 chunk 内容
    # 与 code 完全一致（代码块不以换行结尾，语义更贴近真实源码）。
    code = "    " + "x" * 400 + "\n    y = 1"
    document = _document(code)
    chunks = chunk_document_semantic(
        document, max_chunk_size=100, min_chunk_size=0
    )

    assert len(chunks) == 1
    assert chunks[0].content == code
    _assert_coordinates(chunks, document)


# ── 参数校验与元数据 ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("max_chunk_size", "min_chunk_size"),
    [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
)
def test_semantic_rejects_invalid_window(
    max_chunk_size: int, min_chunk_size: int
) -> None:
    document = _document("正文内容。")
    with pytest.raises(ValueError):
        chunk_document_semantic(
            document,
            max_chunk_size=max_chunk_size,
            min_chunk_size=min_chunk_size,
        )


def test_semantic_empty_document_yields_no_chunks() -> None:
    document = _document("")
    assert chunk_document_semantic(document) == []


def test_semantic_metadata_marks_strategy() -> None:
    """语义分块在 metadata 打策略标记；字符分块保持 S3-T1 元数据不变。"""
    document = _document("正文内容。", metadata={"subject": "ml"})

    semantic_chunks = chunk_document_semantic(document)
    assert semantic_chunks[0].metadata == {
        "subject": "ml",
        "chunking": "semantic",
    }

    # 注意：chunk_documents 是批量函数，必须传「列表」而不是单个文档；
    # pydantic 模型本身可迭代（迭代出 (字段名, 值) 元组），直接传单个
    # 模型会被当成可迭代对象拆成字段元组，导致分块崩溃。
    character_chunks = chunk_documents([document], chunk_size=50, overlap=0)
    assert character_chunks[0].metadata == {"subject": "ml"}


def test_semantic_multi_page_coordinates() -> None:
    """多页文档：每页独立分块，坐标与 chunk_id 携带正确的 page。"""
    documents = [
        KnowledgeDocument(
            document_id="book",
            content="第 1 章 甲\n内容甲。",
            source="book.pdf",
            page=1,
        ),
        KnowledgeDocument(
            document_id="book",
            content="第 2 章 乙\n内容乙。",
            source="book.pdf",
            page=2,
        ),
    ]
    chunks = chunk_documents_semantic(documents, max_chunk_size=1000)

    assert [chunk.page for chunk in chunks] == [1, 2]
    assert chunks[0].chunk_id.startswith("book:1:")
    assert chunks[1].chunk_id.startswith("book:2:")
    _assert_coordinates([chunks[0]], documents[0])
    _assert_coordinates([chunks[1]], documents[1])


# ── 策略参数选择生效（service 层）─────────────────────────────────


def test_service_strategy_parameter_effect() -> None:
    """策略参数生效：semantic 与 character 产出不同 chunk 集，默认不变。"""
    content = (
        "第 1 章 引言\n"
        "介绍。\n"
        "\n"
        "第 2 章 方法\n"
        "方法细节。\n"
    )
    document = KnowledgeDocument(
        document_id="doc", content=content, source="doc.txt"
    )

    default_service = KnowledgeService(
        InMemoryKnowledgeIndex(), chunk_size=30, overlap=5
    )
    character_service = KnowledgeService(
        InMemoryKnowledgeIndex(), chunk_size=30, overlap=5, chunking="character"
    )
    semantic_service = KnowledgeService(
        InMemoryKnowledgeIndex(), chunking="semantic", max_chunk_size=500
    )

    default_chunks = default_service.add_documents([document])
    character_chunks = character_service.add_documents([document])
    semantic_chunks = semantic_service.add_documents([document])

    # character 默认兼容：不传 chunking 与显式 character 产出完全一致。
    assert default_chunks == character_chunks
    # 两种策略产出不同的分块集合。
    assert {chunk.chunk_id for chunk in default_chunks} != {
        chunk.chunk_id for chunk in semantic_chunks
    }
    # semantic 按标题切分：两个 chunk 分别以章节标题开头。
    assert len(semantic_chunks) == 2
    assert semantic_chunks[0].content.startswith("第 1 章")
    assert semantic_chunks[1].content.startswith("第 2 章")


@pytest.mark.parametrize("chunking", ["vector", "", "SEMANTIC"])
def test_service_rejects_unknown_strategy(chunking: str) -> None:
    with pytest.raises(ValueError, match="chunking"):
        KnowledgeService(InMemoryKnowledgeIndex(), chunking=chunking)
