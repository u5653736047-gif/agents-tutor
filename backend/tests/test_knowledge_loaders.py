"""Text and PDF knowledge loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.knowledge.loaders import load_pdf, load_text


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
        "source": str(source),
        "page": None,
        "metadata": {},
    }


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

    documents = load_pdf(source, document_id="physics")

    assert [(item.content, item.page) for item in documents] == [
        ("First page", 1),
        ("Third page", 3),
    ]
    assert all(item.document_id == "physics" for item in documents)
    assert all(item.source == str(source) for item in documents)


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
