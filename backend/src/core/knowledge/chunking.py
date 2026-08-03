"""Small deterministic character-window chunker."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import KnowledgeChunk, KnowledgeDocument


def _validate_window(chunk_size: int, overlap: int) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")


def chunk_document(
    document: KnowledgeDocument,
    *,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[KnowledgeChunk]:
    """Split one document into stable character windows."""
    _validate_window(chunk_size, overlap)

    chunks: list[KnowledgeChunk] = []
    start = 0
    while start < len(document.content):
        end = min(start + chunk_size, len(document.content))
        page = document.page if document.page is not None else 0
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"{document.document_id}:{page}:{start}:{end}",
                document_id=document.document_id,
                content=document.content[start:end],
                source=document.source,
                page=document.page,
                start=start,
                end=end,
                metadata=document.metadata.copy(),
            )
        )
        if end == len(document.content):
            break
        start = end - overlap
    return chunks


def chunk_documents(
    documents: Iterable[KnowledgeDocument],
    *,
    chunk_size: int = 1000,
    overlap: int = 100,
) -> list[KnowledgeChunk]:
    """Chunk documents in input order."""
    _validate_window(chunk_size, overlap)
    return [
        chunk
        for document in documents
        for chunk in chunk_document(document, chunk_size=chunk_size, overlap=overlap)
    ]


# ── 语义分块（S3-T2 新增：按标题/段落边界 + 公式/代码最小保护）──────
#
# 与字符分块相比，语义分块（strategy="semantic"）的差别只在「切分点的
# 选择」上：字符分块按固定窗口滑动切分，语义分块把切分点放在章节标题 /
# 段落边界上，并保证公式段与代码块不被从中间截断。两种策略产出的
# chunk 坐标语义完全一致（见模块 docstring 的坐标约定），因此按坐标
# 回溯原文、chunk_id 派生规则、索引与检索链路都不需要任何改动。
#
# 标题识别规则（启发式，不追求完美解析）：
# 1. Markdown 标题：行首 1-6 个 # 后跟空白与内容（如 "## 支持向量机"）；
# 2. 中文章节标题：行首 "第" + 数字或中文数字 + 章/节/篇/部/分/卷
#    （如 "第 1 章 引言"、"第三章 监督学习"）；
# 3. 数字小节编号：行首数字编号（至少含一个点号）后跟空白与内容
#    （如 "3.2.1 支持向量机"；无点号的 "3 xxx" 不识别，避免把列表项
#    或年份误当标题）。
#    已知取舍（有意接受的误判）：版本号行（如 "2024.5.1 版本说明"）
#    也会命中该模式而被当作标题——启发式分块只追求简单可解释，误判的
#    后果仅是分块粒度变细（该行开启新 chunk），坐标与可回溯性不受影响；
#    不为它增加「版本/年版」等中文字面特判，因为那会误伤
#    "3.2.1 版本管理" 这类真实小节标题（行为由
#    test_semantic_version_number_is_treated_as_heading 锁定）。
# 命中标题的段落总是开启一个新 chunk，标题行完整保留在 chunk 开头。
# 注意必须加 re.MULTILINE：_is_heading 用 re.match(content, pos, endpos)
# 把匹配起点定位到「段落首行」，而非 MULTILINE 模式下 "^" 只锚定整个
# 字符串开头（pos=0），第二个段落起的所有标题都会匹配失败。
_HEADING_RE = re.compile(
    r"^(?:"
    r"#{1,6}\s+\S"
    r"|第\s*[0-9一二三四五六七八九十百千]+\s*[章节篇部分卷]"
    r"|\d+(?:\.\d+)+\s+\S"
    r")",
    re.MULTILINE,
)

# 公式与代码保护启发式（原理见 _protected_spans 注释）：
# 以下「保护块」模式在整页文本上扫描，命中区间内的任何位置都不允许
# 成为 chunk 边界，从而保证公式/代码块不被从中间截断。
_PROTECTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\$.*?\$\$", re.DOTALL),  # LaTeX 显示公式 $$...$$（可跨行）
    re.compile(r"\\\[.*?\\\]", re.DOTALL),  # LaTeX 显示公式 \[...\]
    re.compile(
        r"\\begin\{[A-Za-z*]+\}.*?\\end\{[A-Za-z*]+\}", re.DOTALL
    ),  # LaTeX 环境（equation/align/gather 等）
    re.compile(r"```.*?```", re.DOTALL),  # Markdown 围栏代码块 ```...```
)
# 缩进代码块：连续至少 2 行以 4 个空格或 Tab 开头（如 Python 源码）。
_INDENTED_CODE_LINE = re.compile(r"^(?: {4}|\t)\S")


def _validate_semantic_window(max_chunk_size: int, min_chunk_size: int) -> None:
    """校验语义分块参数：目标块大小为正，最小块大小非负且小于目标大小。"""
    if max_chunk_size <= 0:
        raise ValueError("max_chunk_size must be greater than zero")
    if min_chunk_size < 0 or min_chunk_size >= max_chunk_size:
        raise ValueError(
            "min_chunk_size must satisfy 0 <= min_chunk_size < max_chunk_size"
        )


def _paragraph_spans(content: str) -> list[tuple[int, int]]:
    """按空行切出「段落」区间（原文字符偏移，左闭右开）。

    规则：连续的非空行属于同一段落；两个非空行之间出现空行
    （中间换行符数量 > 1，兼容 \\r\\n）则段落在此分隔。
    段间空行本身不属于任何段落（chunk 坐标仍可精确回溯原文内容）。
    """
    spans: list[tuple[int, int]] = []
    current_start = -1
    current_end = -1
    for match in re.finditer(r"[^\r\n]+", content):
        start, end = match.span()
        if current_start == -1:
            current_start = start
        elif content[current_end:start].count("\n") > 1:
            # 中间隔了至少一个空行 → 新段落从这里开始。
            spans.append((current_start, current_end))
            current_start = start
        current_end = end
    if current_start != -1:
        spans.append((current_start, current_end))
    return spans


def _is_heading(content: str, start: int, end: int) -> bool:
    """判断一个段落是否像标题：只看段落首行是否命中标题模式。"""
    first_line_end = content.find("\n", start, end)
    if first_line_end == -1:
        first_line_end = end
    return bool(_HEADING_RE.match(content, start, first_line_end))


def _protected_spans(content: str) -> list[tuple[int, int]]:
    """扫描全页文本中的公式/代码保护块，返回合并后的区间列表（升序、不重叠）。

    启发式原理（最小保护，不做完美解析）：
    1. 显式边界标记：$$...$$、\\[...\\]、\\begin{..}..\\end{..}、``` 围栏，
       用非贪婪正则跨行匹配，把整个标记区间视为不可切分；
    2. 缩进代码块：连续至少 2 行以 4 空格/Tab 开头的行视为代码块
       （如 Python 源码），遇到非缩进内容结束；
    3. 最后把所有区间排序并合并（重叠或相邻的合并成一个）。
       副作用说明：若正文行恰好行首缩进 4 空格，会被当作代码块保护，
       后果只是 chunk 边界顺延、粒度变大，不影响坐标正确性。
    """
    spans: list[tuple[int, int]] = []
    for pattern in _PROTECTED_PATTERNS:
        spans.extend(match.span() for match in pattern.finditer(content))

    lines = [
        (match.start(), match.group()) for match in re.finditer(r"[^\r\n]+", content)
    ]
    index = 0
    while index < len(lines):
        if _INDENTED_CODE_LINE.match(lines[index][1]):
            end_index = index
            while end_index < len(lines):
                text = lines[end_index][1]
                if text.strip() and not _INDENTED_CODE_LINE.match(text):
                    break
                end_index += 1
            if end_index - index >= 2:  # 至少连续 2 行缩进才算代码块
                block_start = lines[index][0]
                block_end = lines[end_index - 1][0] + len(lines[end_index - 1][1])
                spans.append((block_start, block_end))
            index = end_index
        else:
            index += 1

    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _push_past_protected(boundary: int, protected: list[tuple[int, int]]) -> int:
    """把切分点推到最近的保护块之外。

    原理：若候选切分点落在某个保护块中间（start < p < end），说明在该点
    切分会把公式/代码从中间截断，因此把切分点整体推到保护块结束位置
    （保护块整体并入前一个 chunk；若保护块跨多个段落，后续边界会由
    _finalize_bounds 依次顺延，保证 chunk 不重叠、不丢内容）。
    """
    while True:
        for start, end in protected:
            if start < boundary < end:
                boundary = end
                break
        else:
            return boundary


def _finalize_bounds(
    raw: list[tuple[int, int]], protected: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """对候选边界做保护校正，输出最终互不重叠的 chunk 区间。

    逐个处理原始边界：
    - 起点不早于上一个 chunk 的校正后终点（避免保护推挤造成重叠）；
    - 终点若在保护块内则推到保护块结束（_push_past_protected）；
    - 校正后起点 >= 终点的空区间直接丢弃（内容已被前一个 chunk 覆盖）。
    """
    finalized: list[tuple[int, int]] = []
    previous_end = 0
    for start, end in raw:
        start = max(start, previous_end)
        end = _push_past_protected(end, protected)
        if start >= end:
            continue
        finalized.append((start, end))
        previous_end = end
    return finalized


def _split_oversized_paragraph(
    content: str,
    start: int,
    end: int,
    max_chunk_size: int,
    min_chunk_size: int,
) -> list[tuple[int, int]]:
    """切分单个超长段落（长度超过目标块大小）：字符窗口 + 最近行边界。

    原理：以 max_chunk_size 为窗口滑动；窗口终点前若存在「离窗口较近」
    的行边界（最后一行长度不超过 min_chunk_size），就切在行尾而不是
    句子中间。公式/代码保护由外层 _finalize_bounds 统一校正。
    """
    bounds: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + max_chunk_size, end)
        if window_end < end:
            newline = content.rfind("\n", cursor, window_end)
            if newline != -1 and window_end - newline <= min_chunk_size:
                window_end = newline + 1  # 切到行尾（不含换行符）
        bounds.append((cursor, window_end))
        cursor = window_end
    return bounds


def chunk_document_semantic(
    document: KnowledgeDocument,
    *,
    max_chunk_size: int = 2000,
    min_chunk_size: int = 200,
) -> list[KnowledgeChunk]:
    """按章节标题 / 段落边界分块（strategy="semantic"）。

    切分规则（可测试的启发式）：
    1. 先扫描全页保护块（公式/代码，见 _protected_spans）；
    2. 把文本按空行切成段落；标题段（见 _HEADING_RE）总是开启新 chunk；
    3. 非标题段落按顺序并入当前 chunk，并入后超过 max_chunk_size 时
       在段落起点切分；单个超长段落内部按窗口 + 行边界切分；
    4. 所有候选切分点做保护校正（_finalize_bounds）：切点落在保护块
       中间时推到保护块结束，保护块整体并入前一个 chunk。

    坐标与可回溯性：与字符分块完全一致——start / end 为原文字符偏移
    （左闭右开），chunk.content == document.content[start:end] 恒成立，
    chunk_id 由 document_id + page + start + end 派生，可按坐标定位回
    原文。metadata 复制自文档并追加 "chunking": "semantic" 便于区分策略。
    """
    _validate_semantic_window(max_chunk_size, min_chunk_size)
    content = document.content
    protected = _protected_spans(content)
    paragraphs = _paragraph_spans(content)

    # 第一步：按段落/标题规则生成「候选边界」（不做保护校正）。
    raw: list[tuple[int, int]] = []
    current: int | None = None
    for para_start, para_end in paragraphs:
        if para_end - para_start > max_chunk_size:
            # 超长段落（如整页代码）：先闭合当前 chunk，再在段内窗口切分。
            if current is not None:
                raw.append((current, para_start))
                current = None
            raw.extend(
                _split_oversized_paragraph(
                    content, para_start, para_end, max_chunk_size, min_chunk_size
                )
            )
            continue
        if current is None:
            current = para_start
            continue
        if _is_heading(content, para_start, para_end) or (
            para_end - current > max_chunk_size
        ):
            # 标题段开启新 chunk；否则并入后超限时也在本段起点切分。
            raw.append((current, para_start))
            current = para_start
    if current is not None:
        raw.append((current, paragraphs[-1][1]))

    # 第二步：保护校正（切点不得落在公式/代码块中间）。
    bounds = _finalize_bounds(raw, protected)

    page = document.page if document.page is not None else 0
    chunks: list[KnowledgeChunk] = []
    for start, end in bounds:
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"{document.document_id}:{page}:{start}:{end}",
                document_id=document.document_id,
                content=content[start:end],
                source=document.source,
                page=document.page,
                start=start,
                end=end,
                metadata={**document.metadata, "chunking": "semantic"},
            )
        )
    return chunks


def chunk_documents_semantic(
    documents: Iterable[KnowledgeDocument],
    *,
    max_chunk_size: int = 2000,
    min_chunk_size: int = 200,
) -> list[KnowledgeChunk]:
    """批量语义分块：按输入顺序逐个文档分块（坐标/元数据语义同上）。"""
    _validate_semantic_window(max_chunk_size, min_chunk_size)
    return [
        chunk
        for document in documents
        for chunk in chunk_document_semantic(
            document,
            max_chunk_size=max_chunk_size,
            min_chunk_size=min_chunk_size,
        )
    ]
