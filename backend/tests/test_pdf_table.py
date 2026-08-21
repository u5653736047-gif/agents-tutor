"""S5-B1/B2 PDF 表格结构化提取测试（core/pdf_table.py + 两条消费链路）。

覆盖清单：
1. Markdown 渲染纯函数：表头/分隔行/竖线转义/None 单元格/空行剔除；
2. 工厂降级语义：off → None、非法值 → ValueError、损坏文件 → None；
3. 真实表格 PDF（手工构造的最小带线框 PDF）端到端：页文本 + [表格]
   GFM 小节；
4. 附件链路（B1）：含表 PDF 的组装消息含 Markdown 表格；off 时输出
   与现状一致；表格文本计入同一字符护栏；
5. 入库链路（B2）：iter_pdf_pages 产出的 chunk 含表格 Markdown；off
   时不含有。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from api.attachments import compose_message_with_attachments
from api.schemas import Attachment
from core.knowledge.loaders import iter_pdf_pages
from core.pdf_table import (
    PdfTableExtractor,
    _table_to_markdown,
    open_pdf_table_extractor,
)


def _build_table_pdf(path: Path) -> None:
    """构造最小带表格线框的 PDF（两列表格：Name/Score + Tom/95）。

    手写 PDF 字节而非引入报告类依赖：pdfplumber 的 lines 策略只认
    页面上的矢量线条，与生成工具无关。
    """
    content = (
        b"1 0 0 1 0 0 cm\n"
        b"100 700 m 300 700 l S\n"
        b"100 650 m 300 650 l S\n"
        b"100 600 m 300 600 l S\n"
        b"100 700 m 100 600 l S\n"
        b"200 700 m 200 600 l S\n"
        b"300 700 m 300 600 l S\n"
        b"BT /F1 12 Tf 110 670 Td (Name) Tj ET\n"
        b"BT /F1 12 Tf 210 670 Td (Score) Tj ET\n"
        b"BT /F1 12 Tf 110 620 Td (Tom) Tj ET\n"
        b"BT /F1 12 Tf 210 620 Td (95) Tj ET\n"
    )
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
        (
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj"
        ),
        (
            b"4 0 obj<</Length " + str(len(content)).encode() + b">>stream\n"
            + content
            + b"endstream endobj"
        ),
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for obj in objs:
        offsets.append(len(out))
        out += obj + b"\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer<</Size " + str(len(objs) + 1).encode() + b"/Root 1 0 R>>\n"
        b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    )
    path.write_bytes(out)


# ── 1. Markdown 渲染纯函数 ────────────────────────────────────────


def test_table_to_markdown_renders_gfm_with_escaping() -> None:
    markdown = _table_to_markdown(
        [["名称", "备注"], ["a|b", None], [None, "x\ny"], [], [None, None]]
    )
    lines = markdown.splitlines()
    assert lines[0] == "| 名称 | 备注 |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| a\\|b |  |"
    assert lines[3] == "|  | x y |"
    # 全空行被剔除：总共 2 数据行 + 表头 + 分隔。
    assert len(lines) == 4


def test_table_to_markdown_empty_table_returns_empty() -> None:
    assert _table_to_markdown([]) == ""
    assert _table_to_markdown([[None, ""], ["", None]]) == ""


def test_table_to_markdown_escapes_backslash_before_pipe() -> None:
    """原文含「\\|」时先转义反斜杠再转义竖线，列结构不被静默破坏。

    若只转义竖线，「a\\|b」会产出「a\\\\|b」——GFM 把「\\\\」渲染为字面
    反斜杠，随后的「|」变成未转义分隔符，该行多出一列、后续全部错位。
    """
    markdown = _table_to_markdown([("公式", "片段"), ("a\\|b", "c")])
    lines = markdown.splitlines()
    bs = chr(92)  # 字面反斜杠
    expected_cell = "a" + bs * 3 + "|b"  # 原文 a 反斜杠 竖线 b 的正确转义形态
    assert lines[2] == f"| {expected_cell} | c |"
    # 行列数不变（表头 + 分隔 + 1 数据行）。
    assert len(lines) == 3


# ── 2. 工厂降级语义 ───────────────────────────────────────────────


def test_open_extractor_mode_off_returns_none(tmp_path: Path) -> None:
    source = tmp_path / "any.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    assert open_pdf_table_extractor(source, mode="off") is None


def test_open_extractor_invalid_mode_raises(tmp_path: Path) -> None:
    source = tmp_path / "any.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    try:
        open_pdf_table_extractor(source, mode="auot")
    except ValueError as exc:
        assert "API_PDF_TABLE_MODE" in str(exc)
    else:
        raise AssertionError("invalid mode must raise ValueError")


def test_open_extractor_corrupt_file_degrades_to_none(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf at all")
    assert open_pdf_table_extractor(corrupt, mode="auto") is None


# ── 3. 真实表格 PDF 端到端（依赖真实 pdfplumber，未装则跳过；
# 先例见 test_ocr.py 的 importorskip——全新 dev-only 环境零影响）──────


def test_page_tables_markdown_renders_real_table(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    _build_table_pdf(tmp_path / "table.pdf")
    extractor = PdfTableExtractor.open(tmp_path / "table.pdf")
    try:
        markdown = extractor.page_tables_markdown(1)
    finally:
        extractor.close()
    assert markdown.startswith("[表格]")
    assert "| Name | Score |" in markdown
    assert "| --- | --- |" in markdown
    assert "| Tom | 95 |" in markdown


def test_page_tables_markdown_out_of_range_page_is_empty(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    _build_table_pdf(tmp_path / "table.pdf")
    with PdfTableExtractor.open(tmp_path / "table.pdf") as extractor:
        assert extractor.page_tables_markdown(99) == ""
        assert extractor.page_tables_markdown(0) == ""


# ── 4. 附件链路（B1）─────────────────────────────────────────────


def _attachment(file_id: str, name: str) -> Attachment:
    return Attachment(file_id=file_id, name=name, content_type=None, size=1)


def test_attachment_pdf_tables_enter_message(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    pytest.importorskip("pdfplumber")
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    monkeypatch.delenv("API_PDF_TABLE_MODE", raising=False)
    user_dir = tmp_path / "student-a"
    user_dir.mkdir(parents=True)
    _build_table_pdf(user_dir / "sheet.pdf")

    composed = compose_message_with_attachments(
        "请分析成绩", [_attachment("sheet.pdf", "成绩单.pdf")], "student-a", None
    )

    assert "[表格]" in composed
    assert "| Name | Score |" in composed
    assert "| Tom | 95 |" in composed


def test_attachment_pdf_mode_off_keeps_legacy_output(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """off 时输出与现状逐项一致：无 [表格] 小节。"""
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("API_PDF_TABLE_MODE", "off")
    user_dir = tmp_path / "student-a"
    user_dir.mkdir(parents=True)
    _build_table_pdf(user_dir / "sheet.pdf")

    composed = compose_message_with_attachments(
        "请分析成绩", [_attachment("sheet.pdf", "成绩单.pdf")], "student-a", None
    )

    assert "[表格]" not in composed
    # 精确钉住 pypdf 拍平形态（同一行文字被空格拼接、无 Markdown 管道）。
    assert "Name Score" in composed
    assert "Tom 95" in composed
    assert "| Name |" not in composed


def test_attachment_pdf_invalid_mode_fails_loudly(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """配置拼写错误要暴露：ValueError 直接上抛，不吞成附件错误标注。"""
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("API_PDF_TABLE_MODE", "auot")
    user_dir = tmp_path / "student-a"
    user_dir.mkdir(parents=True)
    _build_table_pdf(user_dir / "sheet.pdf")

    try:
        compose_message_with_attachments(
            "请分析", [_attachment("sheet.pdf", "成绩单.pdf")], "student-a", None
        )
    except ValueError as exc:
        assert "API_PDF_TABLE_MODE" in str(exc)
    else:
        raise AssertionError("invalid mode must raise ValueError")


def test_attachment_table_text_counts_toward_char_budget(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """表格文本计入同一护栏预算：极小上限时截断标注出现。"""
    pytest.importorskip("pdfplumber")
    monkeypatch.setenv("API_UPLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("API_ATTACHMENT_MAX_CHARS", "10")
    user_dir = tmp_path / "student-a"
    user_dir.mkdir(parents=True)
    _build_table_pdf(user_dir / "sheet.pdf")

    composed = compose_message_with_attachments(
        "请分析", [_attachment("sheet.pdf", "成绩单.pdf")], "student-a", None
    )

    assert "[已截断，仅前 10 字符参与处理]" in composed


# ── 5. 入库链路（B2）─────────────────────────────────────────────


def test_iter_pdf_pages_includes_table_markdown(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    pytest.importorskip("pdfplumber")
    _build_table_pdf(tmp_path / "textbook.pdf")
    monkeypatch.delenv("API_PDF_TABLE_MODE", raising=False)

    docs = list(iter_pdf_pages(tmp_path / "textbook.pdf"))

    assert len(docs) == 1
    assert "[表格]" in docs[0].content
    assert "| Name | Score |" in docs[0].content
    assert docs[0].page == 1


def test_iter_pdf_pages_mode_off_excludes_tables(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _build_table_pdf(tmp_path / "textbook.pdf")
    monkeypatch.setenv("API_PDF_TABLE_MODE", "off")

    docs = list(iter_pdf_pages(tmp_path / "textbook.pdf"))

    assert len(docs) == 1
    assert "[表格]" not in docs[0].content
