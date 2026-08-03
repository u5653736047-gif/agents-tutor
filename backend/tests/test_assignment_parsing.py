"""作业 PDF 解析纯函数测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pdf_fixture import write_pdf

from core.assignments.parsing import extract_pdf_text, parse_upload


def test_parse_upload_detects_text_based_pdf_and_page_stats(tmp_path: Path) -> None:
    source = tmp_path / "homework.pdf"
    # 夹具是 latin-1 编码，只能用 ASCII 文本
    first_page = "Question 1: 1+1=2"
    second_page = "Explain gradient descent"
    write_pdf(source, [first_page, second_page, None])

    result = parse_upload(source)

    assert result["ok"] is True
    assert result["file_type"] == "pdf"
    assert result["pdf_type"] == "mixed"
    assert result["page_count"] == 3
    assert result["text_pages"] == 2
    assert result["blank_pages"] == 1
    assert result["pages"] == [
        {"page": 1, "char_count": len(first_page)},
        {"page": 2, "char_count": len(second_page)},
        {"page": 3, "char_count": 0},
    ]
    assert "可继续批改" in result["message"]


def test_parse_upload_reports_no_text_layer_as_structured_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scanned.pdf"
    write_pdf(source, [None])

    result = parse_upload(source)

    assert result["ok"] is False
    assert result["pdf_type"] == "scanned"
    assert "无文本层" in result["message"]


def test_parse_upload_rejects_non_pdf_extension(tmp_path: Path) -> None:
    source = tmp_path / "homework.txt"
    source.write_text("hello", encoding="utf-8")

    result = parse_upload(source)

    assert result["ok"] is False
    assert result["file_type"] == "unsupported"
    assert "仅支持 PDF" in result["message"]


def test_parse_upload_rejects_bad_pdf_magic(tmp_path: Path) -> None:
    source = tmp_path / "fake.pdf"
    source.write_bytes(b"not a pdf at all")

    result = parse_upload(source)

    assert result["ok"] is False
    assert result["file_type"] == "not_pdf"


def test_parse_upload_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        parse_upload(tmp_path / "missing.pdf")


def test_parse_upload_rejects_path_outside_upload_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    write_pdf(outside, ["content"])
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()

    with pytest.raises(ValueError, match="上传目录"):
        parse_upload(outside, upload_root=upload_root)


def test_parse_upload_accepts_path_inside_upload_root(tmp_path: Path) -> None:
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    source = upload_root / "ok.pdf"
    write_pdf(source, ["content"])

    result = parse_upload(source, upload_root=upload_root)

    assert result["ok"] is True


def test_extract_pdf_text_returns_per_page_text(tmp_path: Path) -> None:
    source = tmp_path / "homework.pdf"
    write_pdf(source, ["Question 1", None, "Question 3"])

    result = extract_pdf_text(source)

    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["pages"][0]["text"] == "Question 1"
    assert result["pages"][1]["text"] is None
    assert result["pages"][2]["text"] == "Question 3"


def test_extract_pdf_text_truncates_long_pages(tmp_path: Path) -> None:
    source = tmp_path / "long.pdf"
    write_pdf(source, ["x" * 500])

    result = extract_pdf_text(source, max_chars_per_page=100)

    assert result["ok"] is True
    assert result["truncated"] is True
    assert "已截断" in result["pages"][0]["text"]
