"""Text and PDF knowledge loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.knowledge.loaders import iter_pdf_pages, load_pdf, load_text
from tests.test_pdf_table import _build_table_pdf


def _write_pdf(path: Path, page_texts: list[str | None]) -> None:
    """Write a tiny standards-compliant PDF without adding a test dependency."""
    page_count = len(page_texts)
    first_page_id = 3
    first_content_id = first_page_id + page_count
    font_id = first_content_id + page_count
    page_ids = range(first_page_id, first_content_id)

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in page_ids)}] "
            f"/Count {page_count} >>"
        ).encode(),
    ]
    for offset in range(page_count):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {first_content_id + offset} 0 R >>"
            ).encode()
        )
    for text in page_texts:
        if text is None:
            content = b""
        else:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode()
            + content
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{object_id} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    path.write_bytes(pdf)


def test_load_text_reads_utf8_and_derives_stable_document_id(tmp_path: Path) -> None:
    source = tmp_path / "lesson.txt"
    source.write_text("  牛顿第一定律  ", encoding="utf-8")

    documents = load_text(source)

    assert len(documents) == 1
    assert documents[0].document_id.startswith("lesson:")
    assert documents[0].model_dump(exclude={"document_id"}) == {
        "content": "牛顿第一定律",
        "source": source.name,
        "page": None,
        "metadata": {},
    }


def test_load_text_accepts_a_controlled_source_label(tmp_path: Path) -> None:
    source = tmp_path / "private" / "lesson.txt"
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")

    default_document = load_text(source)[0]
    labeled_document = load_text(source, source_label="course://lesson")[0]

    assert default_document.source == source.name
    assert labeled_document.source == "course://lesson"
    assert default_document.document_id == labeled_document.document_id


@pytest.mark.parametrize("prefix", ["", " "])
def test_load_text_rejects_an_absolute_source_label(
    tmp_path: Path,
    prefix: str,
) -> None:
    source = tmp_path / "private" / "lesson.txt"
    source.parent.mkdir()
    source.write_text("content", encoding="utf-8")

    with pytest.raises(ValidationError, match="logical identifier"):
        load_text(source, source_label=f"{prefix}{source}")


def test_load_text_rejects_empty_content_with_filename(tmp_path: Path) -> None:
    source = tmp_path / "empty.txt"
    source.write_text(" \n\t ", encoding="utf-8")

    with pytest.raises(ValueError, match="empty.txt"):
        load_text(source)


def test_default_document_id_distinguishes_same_stem_in_different_paths(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first" / "lesson.txt"
    second = tmp_path / "second" / "lesson.txt"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    first_id = load_text(first)[0].document_id
    second_id = load_text(second)[0].document_id

    assert first_id != second_id
    assert load_text(first)[0].document_id == first_id


def test_load_pdf_returns_only_nonempty_pages(tmp_path: Path) -> None:
    source = tmp_path / "lesson.pdf"
    _write_pdf(source, ["First page", None, "Third page"])

    documents = load_pdf(
        source,
        document_id="physics",
        source_label="course://physics",
    )

    assert [(item.content, item.page) for item in documents] == [
        ("First page", 1),
        ("Third page", 3),
    ]
    assert all(item.document_id == "physics" for item in documents)
    assert all(item.source == "course://physics" for item in documents)


def test_load_pdf_rejects_document_without_text(tmp_path: Path) -> None:
    source = tmp_path / "blank.pdf"
    _write_pdf(source, [None])

    with pytest.raises(ValueError, match="blank.pdf"):
        load_pdf(source)


def test_load_pdf_wraps_reader_error_with_filename(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"not a pdf")

    with pytest.raises(ValueError, match="broken.pdf"):
        load_pdf(source)


# ── S5-C3：扫描页 OCR 兜底 ────────────────────────────────────────


def _build_no_text_pdf(path: Path) -> None:
    """构造无文本层的 PDF（空内容流页）：模拟扫描版书页。"""
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>endobj",
        b"4 0 obj<</Length 0>>stream\n\nendstream endobj",
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


class _FakeOcr:
    """OCR 替身：固定返回识别文本，可注入异常。"""

    def __init__(self, text: str = "OCR 识别的条件随机场", error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[bytes] = []

    def extract_text(self, image_bytes: bytes) -> str:
        self.calls.append(image_bytes)
        if self.error is not None:
            raise self.error
        return self.text


def test_iter_pdf_pages_ocr_fallback_marks_extraction(tmp_path: Path) -> None:
    # 渲染半段依赖真实 pypdfium2（pdf-table extra 的传递依赖）：纯 dev
    # 最小环境跳过而非红（与 test_pdf_table.py 的守卫先例一致）。
    pytest.importorskip("pypdfium2")
    """无文本层页经 OCR 兜底产出文档，metadata 携带 extraction=ocr 标记。"""
    pdf = tmp_path / "scanned.pdf"
    _build_no_text_pdf(pdf)
    ocr = _FakeOcr()

    docs = list(
        iter_pdf_pages(pdf, document_id="scan", source_label="scan.pdf", ocr_provider=ocr)
    )

    assert len(docs) == 1
    assert docs[0].content == "OCR 识别的条件随机场"
    assert docs[0].metadata == {"extraction": "ocr"}
    assert docs[0].page == 1
    # 渲染出的 PNG 字节确实交给了 provider。
    assert ocr.calls and ocr.calls[0].startswith(b"\x89PNG")


def test_iter_pdf_pages_ocr_failure_skips_page_without_error(tmp_path: Path) -> None:
    """OCR 抛异常 → 该页按无文本现状跳过；全书零文本时抛既有 ValueError。"""
    pdf = tmp_path / "scanned.pdf"
    _build_no_text_pdf(pdf)
    ocr = _FakeOcr(error=RuntimeError("engine down"))

    with pytest.raises(ValueError, match="no extractable text"):
        list(
            iter_pdf_pages(
                pdf, document_id="scan", source_label="scan.pdf", ocr_provider=ocr
            )
        )


def test_iter_pdf_pages_ocr_unavailable_import_falls_back_to_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pypdfium2 导入失败 → 等价于无 OCR：页面静默跳过，不抛 ImportError。"""
    import sys

    pdf = tmp_path / "scanned.pdf"
    _build_no_text_pdf(pdf)
    monkeypatch.setitem(sys.modules, "pypdfium2", None)

    with pytest.raises(ValueError, match="no extractable text"):
        list(
            iter_pdf_pages(
                pdf,
                document_id="scan",
                source_label="scan.pdf",
                ocr_provider=_FakeOcr(),
            )
        )


def test_iter_pdf_pages_with_text_layer_never_invokes_ocr(tmp_path: Path) -> None:
    """有文本层的页面不触发 OCR（零回归保障）。"""
    pdf = tmp_path / "text.pdf"
    _build_table_pdf(pdf)
    ocr = _FakeOcr()

    docs = list(
        iter_pdf_pages(pdf, document_id="t", source_label="t.pdf", ocr_provider=ocr)
    )

    assert docs and ocr.calls == []
