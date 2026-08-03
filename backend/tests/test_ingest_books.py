"""S3-T1 批量入库脚本核心逻辑测试。

覆盖验收标准对应项：
1. 清单解析与校验（必填字段、source 唯一且合法、file 存在、难度枚举、
   verify 用例非空且 expected_source 合法）；
2. 幂等入库（默认 skip-existing：已完成的书跳过，chunk 不重复）；
3. 失败续跑（解析失败不留完成标记，恢复后自动重试成功）；
4. 检索用例验证（期望 source 出现在前 top_k 命中中判 PASS）；
5. 端到端 CLI：用临时目录里的真实小 PDF 走通 入库 → 重跑幂等 → --verify。

所有用例使用 tmp_path 小样本文档（假 page_loader 或真实小 PDF），
不依赖 data/books/ 的真实教材。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from ingest_books import (
    ManifestBook,
    VerifyCase,
    ingest_book,
    load_manifest,
    main,
    select_books,
    verify_cases,
)

from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import SqliteVectorKnowledgeIndex

# ── 小工具：构造清单 JSON 与占位书文件 ────────────────────────────


def _book_entry(source: str, *, file_name: str = "book.pdf", **overrides: object) -> dict:
    """构造一份合法的清单书条目，测试可覆盖任意字段。"""
    entry: dict = {
        "source": source,
        "file": file_name,
        "title": f"《{source}》",
        "authors": ["测试作者"],
        "subjects": ["机器学习"],
        "difficulty": "beginner",
        "verify": [{"query": "支持向量机", "expected_source": source}],
    }
    entry.update(overrides)
    return entry


def _write_manifest(
    tmp_path: Path,
    entries: list[dict],
    *,
    version: int = 1,
    create_files: bool = True,
) -> tuple[Path, Path]:
    """写入清单 JSON 并创建 books_dir；默认给每本书放一个占位 PDF。

    create_files=False 用于「文件不存在」类用例；已存在的文件（如
    _cli_manifest 写的真实小 PDF）不会被占位内容覆盖。
    """
    books_dir = tmp_path / "books"
    books_dir.mkdir(exist_ok=True)
    if create_files:
        for entry in entries:
            target = books_dir / str(entry["file"])
            if not target.exists():
                target.write_bytes(b"%PDF-1.4 placeholder")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"version": version, "books": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path, books_dir


def _manifest_book(
    source: str, *, difficulty: str = "beginner", blocked: str | None = None
) -> ManifestBook:
    """构造内存中的书条目（不落盘，配合假 page_loader 使用）。"""
    return ManifestBook(
        source=source,
        file="book.pdf",
        title=f"《{source}》",
        authors=["测试作者"],
        subjects=["机器学习"],
        difficulty=difficulty,
        blocked=blocked,
        verify=[VerifyCase(query="支持向量机", expected_source=source)],
    )


def _fake_pages(
    path: Path, document_id: str, source_label: str
) -> Iterator[KnowledgeDocument]:
    """假 page_loader：产出两页与 source 强相关的文本。"""
    yield KnowledgeDocument(
        document_id=document_id,
        content="支持向量机 间隔 核函数",
        source=source_label,
        page=1,
    )
    yield KnowledgeDocument(
        document_id=document_id,
        content="条件随机场 概率 标注",
        source=source_label,
        page=2,
    )


def _pages_with(texts: list[str]) -> Callable[[Path, str, str], Iterator[KnowledgeDocument]]:
    """构造假 page_loader：按给定文本列表逐页产出 KnowledgeDocument。

    用于需要「每本书内容互不相同」的检索用例测试——词法检索只命中
    目标书，避免多本书同分时依赖 chunk_id 排序的不确定性。
    """

    def load(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        for page_number, text in enumerate(texts, start=1):
            yield KnowledgeDocument(
                document_id=document_id,
                content=text,
                source=source_label,
                page=page_number,
            )

    return load


@pytest.fixture
def index(tmp_path: Path) -> Iterator[SqliteKnowledgeIndex]:
    """每个测试独立的临时 SQLite 索引。"""
    instance = SqliteKnowledgeIndex(tmp_path / "knowledge.db")
    yield instance
    instance.close()


# ── 清单解析与校验 ────────────────────────────────────────────────


def test_load_manifest_parses_valid_manifest(tmp_path: Path) -> None:
    entries = [_book_entry("ml-a"), _book_entry("dl-b", file_name="b.pdf")]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    manifest = load_manifest(manifest_path, books_dir=books_dir)

    assert manifest.version == 1
    assert [book.source for book in manifest.books] == ["ml-a", "dl-b"]
    assert manifest.books[0].verify[0].expected_source == "ml-a"


def test_load_manifest_rejects_duplicate_source(tmp_path: Path) -> None:
    entries = [_book_entry("ml-a"), _book_entry("ml-a", file_name="b.pdf")]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="source 重复"):
        load_manifest(manifest_path, books_dir=books_dir)


@pytest.mark.parametrize(
    "bad_source",
    ["ML-A", "ml/a", "ml\\a", "ml a", "ml.a", "ml:", "-ml", "ml-"],
)
def test_load_manifest_rejects_invalid_source(
    tmp_path: Path, bad_source: str
) -> None:
    entries = [_book_entry(bad_source)]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="非法 source"):
        load_manifest(manifest_path, books_dir=books_dir)


def test_load_manifest_rejects_missing_file(tmp_path: Path) -> None:
    entries = [_book_entry("ml-a", file_name="does-not-exist.pdf")]
    manifest_path, books_dir = _write_manifest(
        tmp_path, entries, create_files=False
    )

    with pytest.raises(ValueError, match="文件不存在"):
        load_manifest(manifest_path, books_dir=books_dir)


def test_load_manifest_rejects_unknown_difficulty(tmp_path: Path) -> None:
    entries = [_book_entry("ml-a", difficulty="expert")]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="difficulty"):
        load_manifest(manifest_path, books_dir=books_dir)


def test_load_manifest_rejects_empty_verify_cases(tmp_path: Path) -> None:
    entries = [_book_entry("ml-a", verify=[])]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="verify"):
        load_manifest(manifest_path, books_dir=books_dir)


def test_load_manifest_rejects_unknown_expected_source(tmp_path: Path) -> None:
    entries = [
        _book_entry(
            "ml-a",
            verify=[{"query": "问题", "expected_source": "ghost-book"}],
        )
    ]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="expected_source"):
        load_manifest(manifest_path, books_dir=books_dir)


def test_load_manifest_rejects_non_json_file(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="无法读取知识清单"):
        load_manifest(manifest_path, books_dir=tmp_path)


def test_load_manifest_accepts_blocked_field(tmp_path: Path) -> None:
    """blocked 字段：字符串原因被解析；显式 null 与缺省均视为未阻塞。"""
    entries = [
        _book_entry("ml-a", blocked="scanned-pdf-no-text-layer"),
        _book_entry("dl-b", file_name="b.pdf", blocked=None),
        _book_entry("dl-c", file_name="c.pdf"),
    ]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    manifest = load_manifest(manifest_path, books_dir=books_dir)

    assert manifest.books[0].blocked == "scanned-pdf-no-text-layer"
    assert manifest.books[1].blocked is None
    assert manifest.books[2].blocked is None


@pytest.mark.parametrize("bad_blocked", ["", "   ", 123, ["reason"]])
def test_load_manifest_rejects_invalid_blocked(
    tmp_path: Path, bad_blocked: object
) -> None:
    """blocked 存在但非法（空串/非字符串）→ 清单校验失败。"""
    entries = [_book_entry("ml-a", blocked=bad_blocked)]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)

    with pytest.raises(ValueError, match="blocked"):
        load_manifest(manifest_path, books_dir=books_dir)


def test_select_books_subset_and_full(tmp_path: Path) -> None:
    entries = [_book_entry("ml-a"), _book_entry("dl-b", file_name="b.pdf")]
    manifest_path, books_dir = _write_manifest(tmp_path, entries)
    manifest = load_manifest(manifest_path, books_dir=books_dir)

    assert [book.source for book in select_books(manifest, None)] == ["ml-a", "dl-b"]
    # 子集按清单顺序返回，而不是按参数顺序。
    assert [book.source for book in select_books(manifest, "dl-b,ml-a")] == [
        "ml-a",
        "dl-b",
    ]
    with pytest.raises(ValueError, match="未知的 source"):
        select_books(manifest, "nope")


# ── 入库核心：幂等 / 强制重入库 / 失败续跑 ────────────────────────


def test_ingest_skips_completed_book_and_force_reingests(
    index: SqliteKnowledgeIndex, tmp_path: Path
) -> None:
    """默认跳过已完成书（幂等）；--force 后重新入库且 chunk 不重复。"""
    book = _manifest_book("ml-a")
    pdf_path = tmp_path / "book.pdf"
    service = KnowledgeService(index)

    # 1) 首次入库：ingested，写入完成标记。
    first = ingest_book(index, book, pdf_path, page_loader=_fake_pages)
    assert first.status == "ingested"
    assert first.pages == 2
    assert index.is_document_complete("ml-a")

    # 2) 再次默认入库：完成标记存在 → 跳过，分块不重复。
    second = ingest_book(index, book, pdf_path, page_loader=_fake_pages)
    assert second.status == "skipped"
    # 词法索引按「单字 + 双字」匹配：页1 全词命中「支持向量机」（高分），
    # 页2「条件随机场 概率 标注」含单字「机」也被命中（低分）→ 共 2 个 chunk。
    assert len(service.search("支持向量机", top_k=10)) == 2
    # 同理：页2 全词命中「条件随机场」，页1 因单字「机」被命中 → 共 2 个。
    assert len(service.search("条件随机场", top_k=10)) == 2

    # 3) --force 强制重入库：先清标记再入库，分块集合仍不变（整文档替换）。
    forced = ingest_book(index, book, pdf_path, force=True, page_loader=_fake_pages)
    assert forced.status == "ingested"
    # 幂等语义：force 重入库后 chunk 数与内容不变，命中数仍为 2。
    assert len(service.search("支持向量机", top_k=10)) == 2
    assert len(service.search("条件随机场", top_k=10)) == 2


def test_reingest_replaces_stale_content(index: SqliteKnowledgeIndex) -> None:
    """重入库替换旧版本内容：旧词检索不到，新词可命中（S0-T2 语义）。"""
    book = _manifest_book("ml-a")
    pdf_path = Path("unused.pdf")

    def stale_loader(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        yield KnowledgeDocument(
            document_id=document_id,
            content="旧版本内容 苹果",
            source=source_label,
            page=1,
        )

    ingest_book(index, book, pdf_path, page_loader=stale_loader)

    def fresh_loader(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        yield KnowledgeDocument(
            document_id=document_id,
            content="新版本内容 香蕉",
            source=source_label,
            page=1,
        )

    ingest_book(index, book, pdf_path, force=True, page_loader=fresh_loader)

    service = KnowledgeService(index)
    assert service.search("苹果", top_k=5) == []
    assert service.search("香蕉", top_k=5)[0].chunk.document_id == "ml-a"


def test_failed_ingest_leaves_no_mark_and_retry_succeeds(
    index: SqliteKnowledgeIndex, tmp_path: Path
) -> None:
    """失败续跑：解析失败 → 无完成标记；下次运行自动重试成功。"""
    book = _manifest_book("ml-a")
    pdf_path = tmp_path / "book.pdf"
    state = {"broken": True}

    def flaky_loader(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        if state["broken"]:
            raise ValueError("模拟第 3 页解析失败")
        return _fake_pages(path, document_id, source_label)

    with pytest.raises(ValueError, match="解析失败"):
        ingest_book(index, book, pdf_path, page_loader=flaky_loader)
    # 失败后不允许留下完成标记，否则续跑会误跳过这本书。
    assert not index.is_document_complete("ml-a")

    state["broken"] = False
    result = ingest_book(index, book, pdf_path, page_loader=flaky_loader)
    assert result.status == "ingested"
    assert index.is_document_complete("ml-a")
    assert KnowledgeService(index).search("支持向量机", top_k=5)


def test_ingest_skips_blocked_book_without_touching_loader_or_mark(
    index: SqliteKnowledgeIndex,
) -> None:
    """阻塞书：直接返回 blocked，不解析（loader 不被调用）、不写完成标记。"""
    book = _manifest_book("ml-a", blocked="scanned-pdf-no-text-layer")
    calls = {"loader_called": False}

    def exploding_loader(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        calls["loader_called"] = True
        raise AssertionError("blocked 书不应触发解析")

    result = ingest_book(
        index, book, Path("book.pdf"), page_loader=exploding_loader
    )

    assert result.status == "blocked"
    assert not calls["loader_called"]
    assert not index.is_document_complete("ml-a")

    # --force 同样跳过：数据源不可用时强制入库只会报错（恢复 = 移除标记）。
    forced = ingest_book(
        index, book, Path("book.pdf"), force=True, page_loader=exploding_loader
    )
    assert forced.status == "blocked"
    assert not calls["loader_called"]


def test_ingest_injects_domain_metadata(index: SqliteKnowledgeIndex) -> None:
    """学科/难度/书名写入 chunk metadata（S3-T3 元数据过滤的地基）。"""
    book = _manifest_book("ml-a", difficulty="intermediate")
    ingest_book(index, book, Path("book.pdf"), page_loader=_fake_pages)

    hit = KnowledgeService(index).search("支持向量机", top_k=5)[0]

    assert hit.chunk.metadata["subject"] == "机器学习"
    assert hit.chunk.metadata["difficulty"] == "intermediate"
    assert hit.chunk.metadata["title"] == "《ml-a》"


def test_ingest_rejects_empty_pages(index: SqliteKnowledgeIndex) -> None:
    book = _manifest_book("ml-a")

    def empty_loader(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        return iter([])

    with pytest.raises(ValueError, match="解析结果为空"):
        ingest_book(index, book, Path("book.pdf"), page_loader=empty_loader)


def test_progress_callback_receives_page_and_total(tmp_path: Path) -> None:
    """进度回调机制：真实 loader（iter_pdf_pages）逐页回调 (page, total)。"""
    pdf_path = tmp_path / "tiny.pdf"
    _write_tiny_pdf(
        pdf_path, ["Support vector machine interval", "Conditional random field"]
    )
    book = _manifest_book("ml-a")
    calls: list[tuple[int, int]] = []

    def record(page: int, total: int) -> None:
        calls.append((page, total))

    index = SqliteKnowledgeIndex(tmp_path / "kb.db")
    try:
        result = ingest_book(index, book, pdf_path, progress=record)
        assert result.status == "ingested"
    finally:
        index.close()

    assert calls == [(1, 2), (2, 2)]


# ── 检索用例验证 ──────────────────────────────────────────────────


def test_verify_cases_hit_expected_sources(index: SqliteKnowledgeIndex) -> None:
    """每本书的检索用例：查询词命中各自的逻辑 source。

    两本书使用互不重叠的内容，保证词法检索只命中目标书
    （避免多本书同分时依赖 chunk_id 排序的不确定性）。
    """
    service = KnowledgeService(index)
    ingest_book(
        index,
        _manifest_book("ml-a"),
        Path("book.pdf"),
        page_loader=_pages_with(["支持向量机 间隔 核函数"]),
    )
    ingest_book(
        index,
        _manifest_book("dl-b"),
        Path("book.pdf"),
        page_loader=_pages_with(["条件随机场 概率 标注"]),
    )

    cases = [
        VerifyCase(query="支持向量机", expected_source="ml-a"),
        VerifyCase(query="条件随机场", expected_source="dl-b"),
    ]
    results = verify_cases(service, cases, top_k=5)

    assert all(result.passed for result in results)
    # 内容互异 → 首名命中即为期望来源（无同分排序歧义）。
    assert results[0].top_hits[0][0] == "ml-a"
    assert results[1].top_hits[0][0] == "dl-b"


def test_verify_reports_missing_source_as_failed(index: SqliteKnowledgeIndex) -> None:
    """期望 source 未出现在前 top_k 中 → FAIL（验证逻辑不虚报）。"""
    service = KnowledgeService(index)
    ingest_book(
        index,
        _manifest_book("ml-a"),
        Path("book.pdf"),
        page_loader=_fake_pages,
    )

    results = verify_cases(
        service,
        [VerifyCase(query="完全不存在的术语xyz", expected_source="ml-a")],
        top_k=5,
    )

    assert not results[0].passed
    assert results[0].top_hits == []


# ── 端到端 CLI（真实小 PDF 走通 入库 → 幂等 → 验证）───────────────


def _write_tiny_pdf(path: Path, page_texts: list[str]) -> None:
    """写一个合规的最小 PDF（内容用 latin-1 可编码的英文，避免字体编码问题）。

    说明：pypdf 提取中文字体文本依赖字体子集，测试用英文文本保证
    提取确定性；中文检索路径已由上面的假 loader 用例覆盖。
    """
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


def _cli_manifest(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """构造 CLI 端到端环境：清单 + 书目录(含真实小 PDF) + 数据库路径。"""
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    _write_tiny_pdf(
        books_dir / "ml-a.pdf",
        ["Support vector machine interval kernel", "Ensemble learning boosting"],
    )
    entries = [
        _book_entry(
            "ml-a",
            file_name="ml-a.pdf",
            title="Test Book",
            verify=[
                {"query": "support vector machine", "expected_source": "ml-a"},
                {"query": "ensemble learning", "expected_source": "ml-a"},
            ],
        )
    ]
    manifest_path, _ = _write_manifest(tmp_path, entries)
    db_path = tmp_path / "kb.db"
    return manifest_path, books_dir, db_path, tmp_path


def test_main_end_to_end_ingest_reingest_and_verify(tmp_path: Path) -> None:
    """真实小 PDF：入库 → 重跑（幂等跳过）→ --verify 全部 PASS。"""
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        # S3-T5：--vector-db 指向 tmp 路径，隔离真实工作区
        # data/vector_knowledge.db——否则 --verify 会探测到开发者本地
        # 的向量库（存在与否决定混合/降级路径），测试确定性受损。
        "--vector-db",
        str(tmp_path / "vector_knowledge.db"),
    ]

    assert main(common) == 0
    # 第二次运行：完成标记存在 → 跳过，仍成功退出。
    assert main(common) == 0
    # 检索用例验证：两用例全部命中。
    assert main([*common, "--verify"]) == 0

    # 全量验证前先确认数据确实落盘：重开连接检索。
    reopened = SqliteKnowledgeIndex(db_path)
    try:
        assert reopened.is_document_complete("ml-a")
        hits = KnowledgeService(reopened).search("support vector", top_k=5)
        assert hits and hits[0].citation.source == "ml-a"
    finally:
        reopened.close()


def test_main_skips_blocked_book_and_verify_still_succeeds(tmp_path: Path) -> None:
    """main：阻塞书跳过不报错（占位 PDF 不被解析）；--verify 跳过其用例仍成功。"""
    books_dir = tmp_path / "books"
    books_dir.mkdir()
    _write_tiny_pdf(
        books_dir / "ml-a.pdf",
        ["Support vector machine interval kernel", "Ensemble learning boosting"],
    )
    # blocked 书的文件是无效占位：若被解析会失败，从而证明「确实未解析」。
    (books_dir / "ml-b.pdf").write_bytes(b"%PDF-1.4 placeholder")
    entries = [
        _book_entry(
            "ml-a",
            file_name="ml-a.pdf",
            verify=[{"query": "support vector machine", "expected_source": "ml-a"}],
        ),
        _book_entry(
            "ml-b",
            file_name="ml-b.pdf",
            blocked="scanned-pdf-no-text-layer",
            verify=[{"query": "should not run", "expected_source": "ml-b"}],
        ),
    ]
    manifest_path, _ = _write_manifest(tmp_path, entries)
    db_path = tmp_path / "kb.db"
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        # S3-T5：--vector-db 指向 tmp 路径，隔离真实工作区
        # data/vector_knowledge.db——否则 --verify 会探测到开发者本地
        # 的向量库（存在与否决定混合/降级路径），测试确定性受损。
        "--vector-db",
        str(tmp_path / "vector_knowledge.db"),
    ]

    # ingest：ml-a 入库、ml-b 阻塞跳过（占位 PDF 未被解析）→ 退出码 0。
    assert main(common) == 0
    # verify：只验证非阻塞书用例（ml-b 的乱词用例若执行必 FAIL → 退出码 1）。
    assert main([*common, "--verify"]) == 0


def test_main_verify_failure_exit_code(tmp_path: Path) -> None:
    """存在失败用例时 --verify 退出码为 1。"""
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        # S3-T5：--vector-db 指向 tmp 路径，隔离真实工作区
        # data/vector_knowledge.db——否则 --verify 会探测到开发者本地
        # 的向量库（存在与否决定混合/降级路径），测试确定性受损。
        "--vector-db",
        str(tmp_path / "vector_knowledge.db"),
    ]
    # 先正常入库并验证通过（此时 books_dir 里是真实小 PDF）。
    assert main(common) == 0
    assert main([*common, "--verify"]) == 0

    # 覆盖清单：用例查询词保证无命中（verify 模式不解析 PDF，占位文件无碍）
    # → FAIL → 退出码 1。
    entries = [
        _book_entry(
            "ml-a",
            file_name="ml-a.pdf",
            verify=[{"query": "no such term xyzzy", "expected_source": "ml-a"}],
        )
    ]
    failing_manifest, _ = _write_manifest(tmp_path, entries)
    assert main(["--manifest", str(failing_manifest), *common[2:], "--verify"]) == 1


def test_main_rejects_unknown_book_id(tmp_path: Path) -> None:
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)

    code = main(
        [
            "--manifest",
            str(manifest_path),
            "--books-dir",
            str(books_dir),
            "--db",
            str(db_path),
            "--books",
            "ghost-book",
        ]
    )

    assert code == 2


# ── S3-T2 分块策略选择（--chunking semantic | character）──────────


def test_ingest_book_semantic_chunking_selected(
    index: SqliteKnowledgeIndex,
) -> None:
    """ingest_book 支持 chunking="semantic"：按章节标题分块并打策略标记。"""
    book = _manifest_book("ml-a")

    def headed_loader(
        path: Path, document_id: str, source_label: str
    ) -> Iterator[KnowledgeDocument]:
        yield KnowledgeDocument(
            document_id=document_id,
            content=(
                "第 1 章 支持向量机\n\n支持向量机 间隔 核函数\n\n"
                "第 2 章 条件随机场\n\n概率 标注"
            ),
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
    assert result.chunks == 2  # 两章 → 两个 chunk（标题开启新 chunk）
    hit = KnowledgeService(index).search("支持向量机", top_k=5)[0]
    assert hit.chunk.metadata["chunking"] == "semantic"


def test_ingest_default_character_has_no_strategy_marker(
    index: SqliteKnowledgeIndex,
) -> None:
    """默认 character 分块不带策略标记（与 S3-T1 行为一致）。"""
    ingest_book(index, _manifest_book("ml-a"), Path("book.pdf"), page_loader=_fake_pages)

    hit = KnowledgeService(index).search("支持向量机", top_k=5)[0]
    assert "chunking" not in hit.chunk.metadata


def test_main_semantic_chunking_flag(tmp_path: Path) -> None:
    """CLI --chunking semantic：入库成功且 chunk 带策略标记。"""
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        # S3-T5：--vector-db 指向 tmp 路径，隔离真实工作区
        # data/vector_knowledge.db——否则 --verify 会探测到开发者本地
        # 的向量库（存在与否决定混合/降级路径），测试确定性受损。
        "--vector-db",
        str(tmp_path / "vector_knowledge.db"),
    ]

    assert main([*common, "--chunking", "semantic"]) == 0

    reopened = SqliteKnowledgeIndex(db_path)
    try:
        assert reopened.is_document_complete("ml-a")
        hit = KnowledgeService(reopened).search("support vector", top_k=5)[0]
        assert hit.chunk.metadata["chunking"] == "semantic"
    finally:
        reopened.close()


# ── S3-T4 embedding provider 选择（--provider hash | fastembed）──


class _StubFastEmbedProvider:
    """fastembed 的测试替身：512 维确定性向量，不联网不加载模型。

    真实 FastEmbedProvider 首次构造会联网下载 bge-small-zh-v1.5 模型
    （约 100MB），CI/无网环境不可用；替身保持 512 维与真实模型一致，
    用于验证 --provider fastembed 的传参路径与维度语义。
    """

    instances = 0  # 构造计数：验证「向量库不存在时不应构造 provider」

    def __init__(self) -> None:
        type(self).instances += 1
        self.dimension = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        vector = [0.0] * self.dimension
        vector[0] = 1.0  # 非零向量：归一化不会除零
        return [list(vector) for _ in texts]


@pytest.fixture
def stub_fastembed(monkeypatch: pytest.MonkeyPatch) -> type[_StubFastEmbedProvider]:
    """把 ingest_books.FastEmbedProvider 换成测试替身（不联网、可计数）。"""
    _StubFastEmbedProvider.instances = 0
    monkeypatch.setattr("ingest_books.FastEmbedProvider", _StubFastEmbedProvider)
    return _StubFastEmbedProvider


def test_main_provider_rejects_invalid_value(tmp_path: Path) -> None:
    """--provider 非法值：argparse choices 拒绝，退出码 2（SystemExit）。"""
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--manifest",
                str(manifest_path),
                "--books-dir",
                str(books_dir),
                "--db",
                str(db_path),
                "--vector-db",
                str(tmp_path / "vector_knowledge.db"),
                "--provider",
                "bogus",
            ]
        )
    assert excinfo.value.code == 2


@pytest.mark.parametrize("provider", ["hash", "fastembed"])
def test_main_verify_accepts_provider_values(
    tmp_path: Path,
    stub_fastembed: type[_StubFastEmbedProvider],
    provider: str,
) -> None:
    """--verify 接受合法 provider 值；向量库不存在时不构造 provider。

    fastembed 首次构造会联网下载模型——向量库文件不存在时检索注定
    降级为纯词法，此时不应构造 provider（替身构造计数保持 0，证明
    不会白白触发下载）。

    注意：verify 只验证检索不写入，必须先入库（词法库有数据，词法
    检索才有命中基础）；不带 --vector 入库则向量库不会被创建，正
    是「向量库不存在」的验证前提。
    """
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        "--vector-db",
        str(tmp_path / "vector_knowledge.db"),
    ]

    # 先入库（词法库有数据，verify 的词法检索才有命中基础）；不带
    # --vector，向量库不会被创建 → 向量库不存在 → verify 走纯词法。
    assert main(common) == 0
    assert main([*common, "--verify", "--provider", provider]) == 0
    assert _StubFastEmbedProvider.instances == 0


def test_main_fastembed_provider_end_to_end(
    tmp_path: Path, stub_fastembed: type[_StubFastEmbedProvider]
) -> None:
    """fastembed 全流程：入库建 512 维向量库 → 同 provider verify 混合检索。

    验证 --provider 传参正确性：向量库以所选 provider 的维度写入，
    --verify 用同一 provider 能打开并走混合检索（PASS）。
    """
    manifest_path, books_dir, db_path, tmp = _cli_manifest(tmp_path)
    vector_db = tmp / "vector_knowledge.db"
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        "--vector-db",
        str(vector_db),
    ]

    # 1) --vector --provider fastembed：入库成功，向量库以 512 维写入。
    assert main([*common, "--vector", "--provider", "fastembed"]) == 0
    assert vector_db.exists()
    # 用同维度替身重开向量库：构造时维度守卫校验通过（不抛错）即证明
    # 库内向量是 512 维（hash 为 256 维，会因维度不符而拒绝打开）。
    reopened = SqliteVectorKnowledgeIndex(vector_db, provider=_StubFastEmbedProvider())
    reopened.close()

    # 2) --verify --provider fastembed：同一 provider 打开向量库 → 混合检索 PASS。
    assert main([*common, "--verify", "--provider", "fastembed"]) == 0


def test_main_verify_with_fastembed_degrades_on_hash_built_vector_db(
    tmp_path: Path, stub_fastembed: type[_StubFastEmbedProvider]
) -> None:
    """hash 建的 256 维向量库 + fastembed verify：维度不匹配 → 降级纯词法。

    verify 允许降级：旧维度库打不开时自动退回词法单路，不报错、
    退出码 0 仍成立（与 hybrid.py open_vector_index_if_available
    的降级语义一致）；真正换 provider 需 --force 重建向量库。
    """
    manifest_path, books_dir, db_path, tmp = _cli_manifest(tmp_path)
    vector_db = tmp / "vector_knowledge.db"
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        "--vector-db",
        str(vector_db),
    ]

    # 先用默认 hash（256 维）建向量库。
    assert main([*common, "--vector"]) == 0
    # 再用 fastembed（512 维）verify：维度不匹配 → 自动降级纯词法 → 仍 PASS。
    assert main([*common, "--verify", "--provider", "fastembed"]) == 0


class _ExplodingFastEmbedProvider:
    """构造即抛 RuntimeError 的 fastembed 替身：模拟未安装/模型下载失败。"""

    def __init__(self) -> None:
        raise RuntimeError("fastembed 未安装或模型下载失败")


def test_main_vector_with_fastembed_build_failure_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """fastembed 构造失败（未安装/下载失败）：显式报错退出码 2，不静默回退哈希。

    用户显式选了 fastembed，静默换回哈希会与库维度错位——必须让用户
    看到失败原因而不是悄悄降级。
    """
    monkeypatch.setattr("ingest_books.FastEmbedProvider", _ExplodingFastEmbedProvider)
    manifest_path, books_dir, db_path, _ = _cli_manifest(tmp_path)
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        "--vector-db",
        str(tmp_path / "vector_knowledge.db"),
    ]

    assert main([*common, "--vector", "--provider", "fastembed"]) == 2
    captured = capsys.readouterr()
    assert "错误:" in captured.err
    assert "fastembed" in captured.err


def test_main_vector_with_fastembed_against_hash_vector_db_returns_2(
    tmp_path: Path,
    stub_fastembed: type[_StubFastEmbedProvider],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """旧维度（hash 256 维）向量库 + fastembed --vector：显式报错退出码 2。

    维度守卫发生在打开旧库的构造期（早于 --force 清完成标记），
    --force 无法绕过——必须删除向量库文件或改用新的 --vector-db 路径
    重建（错误提示明确给出该指引，且不 traceback 崩溃）。
    """
    manifest_path, books_dir, db_path, tmp = _cli_manifest(tmp_path)
    vector_db = tmp / "vector_knowledge.db"
    common = [
        "--manifest",
        str(manifest_path),
        "--books-dir",
        str(books_dir),
        "--db",
        str(db_path),
        "--vector-db",
        str(vector_db),
    ]

    # 先用默认 hash（256 维）建向量库。
    assert main([*common, "--vector"]) == 0
    # 换 fastembed（512 维）带 --vector 重开旧库：维度不匹配 → 显式报错
    # 退出码 2（不 traceback 崩溃）；旧向量库文件保留、未被破坏。
    assert main([*common, "--vector", "--provider", "fastembed"]) == 2
    assert vector_db.exists()
    captured = capsys.readouterr()
    assert "删除向量库文件" in captured.err
    assert "--force" in captured.err
