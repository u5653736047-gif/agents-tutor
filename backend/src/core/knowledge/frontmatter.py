"""H-T2 前言/目录类 chunk 的启发式识别（向量噪音治理的规则侧）。

背景：真实语义模型下，目录页/讨论链接页等噪音 chunk 与查询向量相似度
偏高（docs/EMBEDDING_SELECTION.md 观察 3）。根因是目录行被切成独立
chunk。本模块提供确定性启发式 classify_frontmatter(content, page)，
ingest 时命中则写 chunk.metadata["chunk_class"] = "frontmatter"，
检索侧默认排除该类 chunk（见 index.py 的 metadata_filter 否定语义
与 service.py 的 suppress_frontmatter）。
"""

from __future__ import annotations

import re

_FRONTMATTER_URL_RE = re.compile(r"discuss\.d2l\.ai", re.IGNORECASE)
# 目录引导点线：连续点（"......"）或点+空格的排版形态
# （d2l 等真实目录是 ". . . ."，点间有空格——真实复测修正）。
_DOT_RUN_RE = re.compile(r"(?:[.…·]\s*){3,}")
_TRAILING_NUM_RE = re.compile(r"\d{1,4}\s*$")    # 行尾页码
_TOC_HEADING_RE = re.compile(
    r"(第\s*[0-9一二三四五六七八九十百千]+\s*[章节篇部分卷]|\d+(?:\.\d+)*\s)"
)   # 目录标题前缀：中文章节号 或 数字编号（行首匹配，见 _is_toc_line）
_SHORT_LINE = 40
_TOC_LINE_LIMIT = 80
_PAGE_LIMIT_TOC = 30      # 目录特征 + 页码靠前阈值
_PAGE_LIMIT_DENSE = 20    # 极短行密度 + 页码靠前阈值
_TOC_RATIO_STRONG = 0.5
_TOC_RATIO_WITH_PAGE = 0.25
_DENSE_SHORT_RATIO = 0.8
# H-T2 偏差说明：极短行密度规则额外要求「非空行数 ≥ 3」——纯页码列
# （i/ii/iii/iv/v）通常多行；不加行数门槛会把单行/两行短文本（如测试
# 里的 "algebra" page=1）误标为目录页，默认抑制会误伤正文。该限定
# 不改变任务正例（5 行）的判定结果，见 test_knowledge_frontmatter。
_DENSE_MIN_LINES = 3
# 讨论链接行的判定门槛（真实复测修正）：d2l 每节末尾都有
# "NNN https://discuss.d2l.ai/t/XXXX" 行，正文页也带——只有「行数极少
# 的碎片页」（几乎全是链接/页码残留）才是噪音，多行正文页不能仅因
# 含一行链接被判 frontmatter（否则默认抑制会误伤正文练习/小结页，
# 见 docs/EMBEDDING_SELECTION.md 实测记录 H-T2 修正）。
_URL_LINES_MAX = 4


def _is_toc_line(line: str) -> bool:
    """单行是否为「目录行」。

    三种形态（满足其一即算目录行；标题前缀一律用行首 match，避免
    把正文/代码行内任意位置的数字+空格误当目录条目）：
    1. 点线引导页码：行内出现 ≥3 个连续点字符（......）且行尾是
       1-4 位数字（目录常见的「条目 ... 页码」排版）；
    2. 标题 + 点线：行首是章节号/数字编号且行内出现点线——真实
       目录排版（如 d2l 的 "9.8.3 束搜索 . . . . ."）点线在行尾、
       行尾是点不是数字、行长可超过 80，故不设行长上限；
    3. 短行 + 行尾页码 + 标题前缀：行长 ≤ 80、行尾是数字、行首是
       标题前缀（如 "10 注意力机制 381"、"1 Introduction 1"）。
    """
    if _DOT_RUN_RE.search(line) and _TRAILING_NUM_RE.search(line):
        return True
    has_heading_prefix = _TOC_HEADING_RE.match(line) is not None
    if has_heading_prefix and _DOT_RUN_RE.search(line):
        return True
    return (
        len(line) <= _TOC_LINE_LIMIT
        and _TRAILING_NUM_RE.search(line) is not None
        and has_heading_prefix
    )


def classify_frontmatter(content: str, page: int | None) -> bool:
    """判断一段文本是否像「前言/目录/讨论链接」类噪音页（chunk 级）。

    判定逻辑（按序，返回第一个命中的结论）：
    1. 含 discuss.d2l.ai 链接且非空行数 ≤ 4 → 讨论链接碎片页（d2l
       每节正文末尾都带一行讨论链接，只有行数极少的碎片页才是噪音，
       见 _URL_LINES_MAX 注释），页码无关；
    2. 统计非空行（行 = line.strip()，过滤空行；无行 → False）：
       - 目录行 is_toc_line(line)：点线引导页码，或短行 + 行尾页码
         + 目录标题前缀；
       - toc_ratio = 目录行数 / 总行数；
       - short_ratio = 行长 ≤ 40 的行数 / 总行数；
    3. toc_ratio ≥ 0.5 → 目录特征主导，不依赖页码；
    4. page 非 None 且 page ≤ 30 且 toc_ratio ≥ 0.25 → 目录特征 +
       页码靠前；
    5. page 非 None 且 page ≤ 20 且 非空行数 ≥ 3 且 short_ratio ≥ 0.8
       → 极短行密度 + 页码靠前（纯页码列，如 i/ii/iii/iv/v）；
    6. 否则不是前言/目录类。
    """
    lines = [line.strip() for line in content.splitlines() if line.strip()]  # 空行不计入统计
    if not lines:
        return False
    # 讨论链接碎片页：行数极少（几乎全是链接/页码残留）才算噪音；
    # 多行正文页仅含一行链接不算（见 _URL_LINES_MAX 注释）。
    if len(lines) <= _URL_LINES_MAX and any(
        _FRONTMATTER_URL_RE.search(line) for line in lines
    ):
        return True

    toc_count = sum(1 for line in lines if _is_toc_line(line))  # 目录行数
    toc_ratio = toc_count / len(lines)  # 目录行占比：越高越像目录页
    short_count = sum(1 for line in lines if len(line) <= _SHORT_LINE)  # 极短行数
    short_ratio = short_count / len(lines)  # 极短行占比：高则像纯页码/残留列

    if toc_ratio >= _TOC_RATIO_STRONG:
        return True
    if (
        page is not None
        and page <= _PAGE_LIMIT_TOC
        and toc_ratio >= _TOC_RATIO_WITH_PAGE
    ):
        return True
    return (
        page is not None
        and page <= _PAGE_LIMIT_DENSE
        and len(lines) >= _DENSE_MIN_LINES
        and short_ratio >= _DENSE_SHORT_RATIO
    )


__all__ = ["classify_frontmatter"]
