"""H-T2 前言/目录类 chunk 启发式识别测试（frontmatter.py + chunking 接入）。

覆盖范围：
1. classify_frontmatter 正例（参数化）：目录行（数字编号/中文章节号/
   点线引导页码）、讨论链接页、极短行密度 + 页码靠前（纯页码列）、
   目录特征主导（不依赖页码）；
2. classify_frontmatter 反例（参数化）：正文章节长文本、参考文献页、
   公式页、无 URL 的 discussion 正文、空串/纯空行、页码不靠前的
   极短行密度；
3. chunk_document / chunk_document_semantic 写入 chunk_class 元数据：
   目录文档写、正文不写、mark_frontmatter=False 关闭。
"""

from __future__ import annotations

import pytest

from core.knowledge.chunking import (
    chunk_document,
    chunk_document_semantic,
)
from core.knowledge.frontmatter import classify_frontmatter
from core.knowledge.models import KnowledgeDocument

# ── 1. classify_frontmatter 正例 ─────────────────────────────────


@pytest.mark.parametrize(
    ("content", "page"),
    [
        # 目录特征主导（toc_ratio = 1）：不依赖页码，page=None 也识别。
        ("1 Introduction 1\n2 Fundamentals 15\n3 Deep Networks 40", None),
        ("第1章 引言 1\n第2章 感知机 20", None),
        # 点线引导页码（目录引导点 ......）。
        ("1 Introduction ....... 1\n2 Fundamentals ..... 15", None),
        # 讨论链接碎片页：行数极少（页码无关）。
        ("Discussions\ndiscuss.d2l.ai/t/conv-nets/1234", None),
        # 真实目录排版（d2l）：标题 + 空格点线，行尾是点不是数字、
        # 行长可超过 80（真实复测修正，见 docs/EMBEDDING_SELECTION.md）。
        (
            (
                "9.8.3 束搜索 . . . . . . . . . . . . . . . . . . . . . . . . . . . . .\n"
                "9.8.4 结论 . . . . . . . . . . . . . . . . . . . . . . . . . . . . . .\n"
                "10 注意力机制 381"
            ),
            10,
        ),
        # 超长目录行（>80 字符）同样识别：形态 2（标题前缀+点线）不设
        # 行长上限，覆盖真实排版的长目录行。
        (
            (
                "10.1.1 生物学中的注意力提示 . . . . . . . . . . . . . . . . . . . . . . . . . . "
                ". . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . ."
            ),
            None,
        ),
        # 极短行密度 + 页码靠前：纯页码列（i/ii/iii/iv/v），5 行。
        ("i\nii\niii\niv\nv", 5),
        # 目录特征 + 页码靠前。
        ("1 Introduction 1\n2 Fundamentals 15", 10),
        # 目录特征主导（规则 3）不依赖页码：页码靠后同样识别
        # （与「页码靠前」用例对照，锁定规则 3 的语义）。
        ("1 Introduction 1\n2 Fundamentals 15", 200),
    ],
)
def test_classify_frontmatter_true(content: str, page: int | None) -> None:
    assert classify_frontmatter(content, page) is True


# ── 2. classify_frontmatter 反例 ─────────────────────────────────


@pytest.mark.parametrize(
    ("content", "page"),
    [
        # 正文章节长文本：长行不满足目录行/短行特征。
        (
            (
                "卷积神经网络（Convolutional Neural Network,CNN）是一种专门处理"
                "网格结构数据的深度学习模型。\n它通过卷积核提取局部特征。"
            ),
            100,
        ),
        # 参考文献页：长行 + 页码不靠前。
        (
            (
                "Krizhevsky, A., et al. (2012). ImageNet classification with deep "
                "convolutional neural networks."
            ),
            300,
        ),
        # 公式页：短行但行尾不是数字、页码不靠前。
        ("x = w^T x + b\ny = softmax(z)", 100),
        # 代码行内任意位置有「数字+空格」且行尾是数字，但行首不是
        # 标题前缀（真实复测修正：目录行前缀必须行首 match）。
        ("return 2 * torch.sin(x) + x**0.8", 100),
        # 正文公式行含空格点线（省略用法）但行首不是标题前缀：
        # 空格点线正则不误伤正文（真实复测修正的防御反例）。
        (
            (
                "简单起见，考虑下面这个回归问题：给定的成对 {(x1, y1), . . . ,(xn, yn)}，"
                "如何学习 f 预测任意新输入 x 的输出？"
            ),
            100,
        ),
        # 多行正文页含一行讨论链接（d2l 每节末尾都有）不是噪音页
        # （真实复测修正：行数门槛 _URL_LINES_MAX，正文页不被误标）。
        (
            (
                "练习\n1. 在机器翻译中通过解码序列词元时，其自主性提示可能是"
                "什么？非自主性提示和感官输入又是什么？\n2. 随机生成一个10× "
                "10矩阵并使用softmax运算来确保每行都是有效的概率分布， 然后"
                "可视化输出注意力\n权重。\n118 https://discuss.d2l.ai/t/5764"
            ),
            404,
        ),
        # 含 discussion 单词但无 URL 的正文：不触发链接规则。
        ("discussion 是正文里的讨论段落，不含链接地址。", 100),
        # 空串 / 纯空行：无非空行。
        ("", None),
        ("   \n\n  ", None),
        # 极短行密度但页码不靠前（规则 5 的页码门槛）。
        ("i\nii\niii\niv\nv", 200),
    ],
)
def test_classify_frontmatter_false(content: str, page: int | None) -> None:
    assert classify_frontmatter(content, page) is False


# ── 3. chunking 接入：chunk_class 元数据 ──────────────────────────


def test_chunk_document_marks_frontmatter_metadata() -> None:
    """目录文本文档：每个 chunk 都带 chunk_class=frontmatter。"""
    document = KnowledgeDocument(
        document_id="book",
        content="1 Introduction 1\n2 Fundamentals 15\n3 Deep Networks 40",
        source="book.txt",
        page=5,
    )
    chunks = chunk_document(document, chunk_size=100, overlap=0)

    assert chunks
    assert all(chunk.metadata["chunk_class"] == "frontmatter" for chunk in chunks)


def test_chunk_document_keeps_plain_body_unmarked() -> None:
    """正文文档（无目录特征）：不写 chunk_class 键。"""
    document = KnowledgeDocument(
        document_id="book",
        content=(
            "卷积神经网络是一种专门处理网格结构数据的深度学习模型，"
            "它通过卷积核提取局部特征。"
        ),
        source="book.txt",
        page=100,
    )
    chunks = chunk_document(document, chunk_size=100, overlap=0)

    assert chunks
    assert all("chunk_class" not in chunk.metadata for chunk in chunks)


def test_chunk_document_mark_frontmatter_false_disables() -> None:
    """mark_frontmatter=False：目录文档也不写 chunk_class。"""
    document = KnowledgeDocument(
        document_id="book",
        content="1 Introduction 1\n2 Fundamentals 15",
        source="book.txt",
        page=5,
    )
    chunks = chunk_document(
        document, chunk_size=100, overlap=0, mark_frontmatter=False
    )

    assert chunks
    assert all("chunk_class" not in chunk.metadata for chunk in chunks)


def test_chunk_document_semantic_marks_frontmatter() -> None:
    """语义分块同样写 chunk_class（与字符分块同一套启发式）。"""
    document = KnowledgeDocument(
        document_id="book",
        content="1 Introduction 1\n2 Fundamentals 15",
        source="book.txt",
        page=5,
    )
    chunks = chunk_document_semantic(document, max_chunk_size=1000)

    assert chunks
    assert all(chunk.metadata["chunk_class"] == "frontmatter" for chunk in chunks)
