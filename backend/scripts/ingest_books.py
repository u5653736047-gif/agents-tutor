"""S3-T1 批量入库脚本：把 data/books/ 的 5 本 AI 学科教材解析并写入 SQLite 知识库。

用法（在 backend/ 目录下，使用项目 venv）：
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py --books ml-lihang,dl-d2l
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py --force
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py --verify
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py --chunking semantic
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py --vector --provider fastembed
    $env:PYTHONPATH="src"; .venv/Scripts/python.exe scripts/ingest_books.py --relabel-frontmatter

设计说明（按功能模块）：
1. 入库流程
   每本书的入库 = 解析（逐页提取文本，带进度打印）→ 一次性调用
   KnowledgeService.add_documents 分块入库 → 写入「完成标记」。
   解析与入库复用 core/knowledge 现有能力：iter_pdf_pages（loaders）、
   chunk_documents + KnowledgeService（service）、SqliteKnowledgeIndex（index），
   不重复造轮子。
2. 幂等机制
   document_id 与逻辑 source 相同（如 ml-lihang）。chunk_id 由 document_id
   派生，相同内容产生相同 chunk_id；add_documents 复用 S0-T2 的「同一
   document_id 整文档替换」语义（先删旧 chunk 再插入新 chunk），因此重复
   执行同一本书的入库不会产生重复 chunk，也不会残留旧版本内容。
3. 续跑机制（大文件失败后重跑）
   「完成标记」表（ingest_marks）只在这本书全部页解析、全部 chunk 写入
   成功后才写入。默认 --skip-existing 模式：已有完成标记的书直接跳过
   （已入库的书不重复解析，这是 190MB AIMA 等大文件的断点续跑关键）。
   --force 重入库前先清除标记：若中途失败，旧标记不存在，下次默认运行
   会自动重新入库这本书；而其它已完成的书仍被跳过，互不影响。
   单本书解析失败只影响它自己（逐书 try/except 继续处理后续书）。
   注意：完成标记不记录分块策略——更换 --chunking 后旧标记仍存在，
   默认运行会直接跳过（新策略不生效）；更换分块策略必须加 --force
   强制重新入库。
4. 逻辑 source 约定
   source 就是清单里的逻辑标识（ml-zhouzhihua 等），与 S0-T1 脱敏语义
   一致：对外只暴露逻辑标识，绝不暴露文件系统路径；同时注入 metadata
   （学科 subject、难度 difficulty、书名 title）供 S3-T3 元数据过滤。
   S3-T3 起：章节字段 chapter/section 与概念标签 tags 由分块层
   （chunking.py）从标题行规则提取并自动附加到每个 chunk 的 metadata，
   本脚本无需额外逻辑——清单注入字段与规则提取字段合起来构成完整的
   领域元数据（字段约定见 core/knowledge/models.py 模块注释）。
5. 检索用例验证（--verify）
   遍历清单中每本书的 verify 用例：执行混合检索（S3-T5 起）——词法
   库必开，向量库文件存在时词法 + 向量两路按 RRF 融合排序（能命中
   同义表述），向量库不存在或打不开时自动降级为纯词法（行为与
   S3-T1 完全一致，不抛错）。期望命中的逻辑 source 出现在前 top_k
   个结果中即 PASS，否则 FAIL；退出码 0 表示全部通过，1 表示存在
   失败用例。
6. blocked 阻塞语义
   清单条目可带 "blocked": "<原因字符串>" 标记某本书当前不可入库
   （例如扫描版 PDF 无文本层，pypdf 提取不到内容）。被标记的书：
   入库与 --verify 均直接跳过（打印原因，计入成功不计入失败，
   --force 也不会尝试入库——数据源不可用时强制入库只会报错）。
   恢复方式：拿到可用的数据源（如文本版 PDF）后移除/清空 blocked
   字段即可自动恢复入库与验证。
7. 分块策略选择（S3-T2）
   --chunking 参数二选一：character（默认）保持 S3-T1 的字符窗口
   分块，行为完全不变；semantic 按章节标题/段落边界分块，并对公式段
   与代码块做最小保护（不被从中间截断）。策略透传给
   KnowledgeService 的 chunking 参数；两种策略产出的 chunk 坐标
   （document_id/page/start/end）语义一致，均可回溯到原文。
8. 向量索引（S3-T4，可选）
   --vector 开关：入库时同步把每个分块写入独立的向量库
   （默认 data/vector_knowledge.db，与词法库 data/knowledge.db 并列，
   都在 data/ 下、不进 git）。向量库存分块原文 + 归一化后的向量
   （Embedding 默认内置哈希替身，离线零依赖；真实语义模型接入方式见
   docs/EMBEDDING_SELECTION.md）。向量索引与词法索引是两份独立数据：
   - 已入库的书（完成标记存在）带 --vector 重跑时，自动从词法库读出
     该书全部分块补写向量（增量构建，不重新解析 PDF）；
   - --force --vector 重入库时，词法与向量同步整文档替换；
   - --verify 默认走混合检索（S3-T5）：词法路必开，向量库文件存在
     才启用（open_vector_index_if_available，打不开自动降级，详见
     core/knowledge/hybrid.py 模块注释）。
   embedding provider 选择（--provider 参数）：hash（默认）是内置
     字符哈希替身（256 维，离线零依赖，语义能力有限的降级方案）；
     fastembed 是真实语义模型 BAAI/bge-small-zh-v1.5（512 维，首次
     使用联网下载模型约 100MB，之后完全离线）。两者维度不同：更换
     provider 后旧向量库打开时维度校验不过：--vector 路径显式报错，
     需删除向量库文件（或改用新的 --vector-db 路径）后重建——--force
     无法绕过维度守卫（守卫发生在打开旧库时，早于清完成标记）；
     --verify 遇旧维度库会自动降级为纯词法（不报错，验证本来允许
     降级），详见 core/knowledge/embedding.py 与
     docs/EMBEDDING_SELECTION.md。
   语义检索与词法检索的并存关系、协议设计见 core/knowledge/vector_index.py
   模块注释与选型文档。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from core.knowledge.embedding import (
    EmbeddingProvider,
    FastEmbedProvider,
    HashEmbeddingProvider,
)
from core.knowledge.frontmatter import classify_frontmatter
from core.knowledge.hybrid import HybridKnowledgeIndex, open_vector_index_if_available
from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.loaders import iter_pdf_pages
from core.knowledge.models import KnowledgeDocument
from core.knowledge.service import KnowledgeService
from core.knowledge.vector_index import SqliteVectorKnowledgeIndex

# ── 路径与常量约定 ───────────────────────────────────────────────
# 脚本位于 backend/scripts/，向上两级即仓库根；数据目录与数据库都放在
# 仓库根 data/ 下（data/ 整体不进 git，PDF 本体与索引库都只存在于本地）。
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "knowledge_manifest.json"
DEFAULT_BOOKS_DIR = REPO_ROOT / "data" / "books"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "knowledge.db"
DEFAULT_VECTOR_DB_PATH = REPO_ROOT / "data" / "vector_knowledge.db"

# 逻辑 source 标识的合法形式：小写字母开头，只含小写字母/数字/连字符，
# 且连字符不能出现在首尾或连续出现（如 ml-、ml--a 均非法）。
_SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
# 难度枚举：与清单文件中的取值一一对应。
_DIFFICULTIES = frozenset({"beginner", "intermediate", "advanced"})
# 每解析多少页打印一次进度（190MB 的 AIMA 约 1200 页，避免刷屏）。
_PROGRESS_EVERY = 25


# ── 清单模型与校验 ───────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyCase:
    """单个检索用例：查询词 + 期望命中的逻辑 source。"""

    query: str
    expected_source: str


@dataclass(frozen=True)
class ManifestBook:
    """清单中一本书的完整信息。"""

    source: str  # 逻辑 source 标识，同时用作 document_id 与完成标记键
    file: str  # data/books/ 下的实际文件名（仅用于定位本地 PDF）
    title: str
    authors: list[str]
    subjects: list[str]
    difficulty: str
    blocked: str | None = None  # 阻塞原因（如扫描版无文本层）；None = 未阻塞
    verify: list[VerifyCase] = field(default_factory=list)


@dataclass(frozen=True)
class Manifest:
    """知识源清单：books 保持清单文件中的顺序。"""

    version: int
    books: list[ManifestBook]

    def sources(self) -> list[str]:
        return [book.source for book in self.books]


def _validate_source_identifier(value: str) -> str:
    """校验逻辑 source 标识：只允许安全的小写标识，天然拒绝路径/空白。

    与 S0-T1 的脱敏语义一致——source 里出现路径分隔符、空白、点号、
    首尾/连续连字符等一律视为非法，尽早失败而不是等到写库时才被
    pydantic 拦截。
    """
    if not _SOURCE_PATTERN.fullmatch(value):
        raise ValueError(
            f"非法 source 标识 {value!r}：必须是小写字母开头，"
            "只含小写字母/数字/连字符（禁止路径、空格等字符）"
        )
    return value


def load_manifest(path: str | Path, *, books_dir: str | Path) -> Manifest:
    """读取并校验知识源清单 JSON，返回 Manifest。

    校验项：必填字段、source 唯一且合法、file 存在于 books_dir、
    难度枚举合法、verify 用例非空且 expected_source 存在于清单、
    blocked 字段（可选）存在时必须是原因字符串（null/缺省 = 未阻塞）。
    """
    manifest_path = Path(path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取知识清单 {manifest_path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("version"), int):
        raise ValueError("知识清单缺少整数 version 字段")
    raw_books = raw.get("books")
    if not isinstance(raw_books, list) or not raw_books:
        raise ValueError("知识清单的 books 必须是非空数组")

    books_dir_path = Path(books_dir)
    books: list[ManifestBook] = []
    seen_sources: set[str] = set()
    for index, entry in enumerate(raw_books):
        if not isinstance(entry, dict):
            raise ValueError(f"第 {index + 1} 本书的条目必须是对象")
        source = _validate_source_identifier(str(entry.get("source", "")))
        if source in seen_sources:
            raise ValueError(f"source 重复：{source!r}")
        seen_sources.add(source)

        file_name = str(entry.get("file", ""))
        if not file_name or "/" in file_name or "\\" in file_name:
            raise ValueError(f"{source}: file 必须是不含路径分隔符的文件名")
        if not (books_dir_path / file_name).is_file():
            raise ValueError(f"{source}: 文件不存在于 {books_dir_path}：{file_name}")

        title = str(entry.get("title", "")).strip()
        authors = [str(item) for item in entry.get("authors", [])]
        subjects = [str(item) for item in entry.get("subjects", [])]
        difficulty = str(entry.get("difficulty", ""))
        if not title:
            raise ValueError(f"{source}: title 不能为空")
        if not authors:
            raise ValueError(f"{source}: authors 至少一个作者")
        if not subjects:
            raise ValueError(f"{source}: subjects 至少一个学科标签")
        if difficulty not in _DIFFICULTIES:
            raise ValueError(
                f"{source}: difficulty 必须是 {sorted(_DIFFICULTIES)} 之一，"
                f"实际为 {difficulty!r}"
            )

        # blocked 可选：存在（非 null）时必须是非空原因字符串。
        raw_blocked = entry.get("blocked")
        if raw_blocked is not None:
            if not isinstance(raw_blocked, str) or not raw_blocked.strip():
                raise ValueError(
                    f"{source}: blocked 必须是阻塞原因字符串（非空），"
                    "或省略/null 表示未阻塞"
                )
            blocked: str | None = raw_blocked
        else:
            blocked = None

        books.append(
            ManifestBook(
                source=source,
                file=file_name,
                title=title,
                authors=authors,
                subjects=subjects,
                difficulty=difficulty,
                blocked=blocked,
                verify=[],
            )
        )

    # 第二遍：所有 source 已收集齐，再校验每本书的检索用例
    # （expected_source 允许指向清单中任意一本书，包括后面才出现的条目）。
    all_sources = {book.source for book in books}
    finalized: list[ManifestBook] = []
    for book, entry in zip(books, raw_books):
        raw_verify = entry.get("verify", [])
        if not isinstance(raw_verify, list) or not raw_verify:
            raise ValueError(f"{book.source}: verify 至少一个检索用例")
        verify: list[VerifyCase] = []
        for case_index, case in enumerate(raw_verify):
            if not isinstance(case, dict):
                raise ValueError(f"{book.source}: 第 {case_index + 1} 个用例必须是对象")
            query = str(case.get("query", "")).strip()
            expected = str(case.get("expected_source", ""))
            if not query:
                raise ValueError(f"{book.source}: 第 {case_index + 1} 个用例 query 不能为空")
            if expected not in all_sources:
                raise ValueError(
                    f"{book.source}: 用例 expected_source {expected!r} "
                    "不在清单的 source 列表中"
                )
            verify.append(VerifyCase(query=query, expected_source=expected))
        finalized.append(
            ManifestBook(
                source=book.source,
                file=book.file,
                title=book.title,
                authors=book.authors,
                subjects=book.subjects,
                difficulty=book.difficulty,
                blocked=book.blocked,
                verify=verify,
            )
        )
    return Manifest(version=raw["version"], books=finalized)


def select_books(manifest: Manifest, selection: str | None) -> list[ManifestBook]:
    """按 --books 参数（逗号分隔的 source 子集）选择书；None 表示全部。"""
    if selection is None or not selection.strip():
        return list(manifest.books)
    wanted = [item.strip() for item in selection.split(",") if item.strip()]
    known = manifest.sources()
    unknown = [item for item in wanted if item not in known]
    if unknown:
        raise ValueError(
            f"未知的 source：{', '.join(unknown)}；可用：{', '.join(known)}"
        )
    return [book for book in manifest.books if book.source in wanted]


# ── 入库核心（可注入 page_loader 以便测试，不依赖真实 PDF）────────


@dataclass(frozen=True)
class IngestResult:
    """一本书的入库结果：status 为 ingested/skipped/blocked 之一。"""

    book_source: str
    status: str
    pages: int = 0
    chunks: int = 0


# page_loader 契约：接收 (PDF 路径, document_id, source_label)，产出该书的
# 全部页文档。默认用 core 的 iter_pdf_pages；测试可注入假 loader。
PageLoader = Callable[[Path, str, str], Iterator[KnowledgeDocument]]


def ingest_book(
    index: SqliteKnowledgeIndex,
    book: ManifestBook,
    pdf_path: Path,
    *,
    force: bool = False,
    page_loader: PageLoader | None = None,
    progress: Callable[[int, int], None] | None = None,
    chunking: str = "character",
    vector_index: SqliteVectorKnowledgeIndex | None = None,
) -> IngestResult:
    """入库一本书，返回入库结果。

    幂等/续跑流程：
    1. 阻塞书（清单 blocked 字段非空）直接返回 blocked，不解析不入库
       （含 --force；数据源不可用时强制入库只会报错，恢复方式见模块注释）；
    2. 默认模式：已有完成标记 → 直接跳过（不重新解析，节省大文件时间）；
       若同时提供了 vector_index 且该书在向量库中还没有分块，则从词法库
       读出已有 chunk 补写向量（增量构建向量索引，无需重新解析 PDF）；
    3. --force：先清除完成标记再重新入库，中途失败则无标记，
       下次默认运行会重新尝试（断点续跑语义）；
    4. 解析全部页 → 注入学科/难度/书名 metadata → 一次性 add_documents
       （S0-T2 整文档替换：旧 chunk 先删后插，无残留）；
       若提供了 vector_index：同步维护向量库（先删该书旧向量再写新向量，
       与词法库的整文档替换语义一致——向量索引是独立数据，需手动同步）；
    5. 全部成功后才写完成标记。

    分块策略（S3-T2）：chunking 参数（"character" 默认 | "semantic"）
    透传给 KnowledgeService 构造，与 CLI --chunking 一一对应；
    默认 character 与 S3-T1 行为完全一致。

    向量索引（S3-T4）：vector_index 参数可选，传入时该书的向量分块
    与词法分块同步写入；不传则纯词法入库（与 S3-T1 行为完全一致）。
    """
    if book.blocked:
        return IngestResult(book_source=book.source, status="blocked")

    if not force and index.is_document_complete(book.source):
        if vector_index is not None and not vector_index.has_document(book.source):
            # 增量补建向量（S3-T4）：词法库已入库但向量库缺失——例如
            # 先前入库未带 --vector。直接从词法库读出该书的全部 chunk
            # 原样补写向量索引，不重新解析 PDF（大文件省时关键）。
            vector_index.upsert(index.chunks_of_document(book.source))
        return IngestResult(book_source=book.source, status="skipped")

    if force:
        # 先清标记：若本次入库中途失败，保证下次默认运行不会误跳过。
        index.clear_document_complete(book.source)

    loader = page_loader or _default_page_loader(progress)
    pages = list(loader(pdf_path, book.source, book.source))
    if not pages:
        raise ValueError(f"{book.source}: 解析结果为空，拒绝入库")

    # 注入领域 metadata：学科、难度、书名（供 S3-T3 元数据过滤检索使用）。
    subject_tags = ",".join(book.subjects)
    for page in pages:
        page.metadata["subject"] = subject_tags
        page.metadata["difficulty"] = book.difficulty
        page.metadata["title"] = book.title

    service = KnowledgeService(index, chunking=chunking)
    chunks = service.add_documents(pages)
    if vector_index is not None:
        # 向量索引与词法索引是两份独立数据（不同数据库文件），整文档替换
        # 语义需要手动同步：先删该书旧向量，再写入新向量（与上方
        # add_documents 内部「先删后插」的顺序一致，中途失败不残留旧版）。
        vector_index.delete_document(book.source)
        vector_index.upsert(chunks)
    index.mark_document_complete(
        book.source, chunk_count=len(chunks), page_count=len(pages)
    )
    return IngestResult(
        book_source=book.source,
        status="ingested",
        pages=len(pages),
        chunks=len(chunks),
    )


def _default_page_loader(
    progress: Callable[[int, int], None] | None,
) -> PageLoader:
    """默认 loader：复用 core loaders.iter_pdf_pages（惰性逐页解析）。"""

    def load(path: Path, document_id: str, source_label: str) -> Iterator[KnowledgeDocument]:
        return iter_pdf_pages(
            path,
            document_id=document_id,
            source_label=source_label,
            progress=progress,
        )

    return load


# ── 检索用例验证 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyResult:
    """一个检索用例的验证结果：top_hits 为 (source, score) 前 top_k 列表。"""

    expected_source: str
    query: str
    passed: bool
    top_hits: list[tuple[str, float]]


def verify_cases(
    service: KnowledgeService,
    cases: list[VerifyCase],
    *,
    top_k: int = 5,
) -> list[VerifyResult]:
    """逐用例执行检索：期望 source 出现在前 top_k 结果中即判 PASS。"""
    results: list[VerifyResult] = []
    for case in cases:
        hits = service.search(case.query, top_k)
        top_hits = [(hit.citation.source, hit.score) for hit in hits]
        passed = any(source == case.expected_source for source, _ in top_hits)
        results.append(
            VerifyResult(
                expected_source=case.expected_source,
                query=case.query,
                passed=passed,
                top_hits=top_hits,
            )
        )
    return results


# ── H-T2 前言/目录增量重标注 ────────────────────────────────────


def _update_metadata_json(
    db_path: Path, table: str, updates: list[tuple[str, str]]
) -> None:
    """直接 UPDATE 某个库表的 metadata_json 列（单事务提交，幂等）。

    table 只取内部常量（"chunks" / "chunk_vectors"），不是用户输入，
    无 SQL 注入面；值一律绑定参数。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            f"UPDATE {table} SET metadata_json = ? WHERE chunk_id = ?",
            [(metadata_json, chunk_id) for chunk_id, metadata_json in updates],
        )
        conn.commit()
    finally:
        conn.close()


def relabel_frontmatter(
    lexical_db: Path, vector_db: Path, books: list[ManifestBook]
) -> int:
    """H-T2：对已入库 chunk 重新执行前言/目录分类，更新两库 metadata_json。

    背景（面向初学者）：H-T2 起分块层默认写 metadata["chunk_class"] =
    "frontmatter"（frontmatter.classify_frontmatter 启发式），检索侧
    默认抑制该类 chunk；但已经入库的旧数据没有该键。本函数把旧库里的
    chunk 逐个重新分类并直接 UPDATE 两库的 metadata_json 列：

    1. 对每本非阻塞书，用 SqliteKnowledgeIndex.chunks_of_document 读出
       全部 chunk（不重新解析 PDF）；
    2. 每个 chunk 计算 classify_frontmatter(content, page)，与现有
       metadata 中的 chunk_class 比对，收集需要更新的 (chunk_id,
       新 metadata_json)——分类结果与标记双向一致（新标 frontmatter
       或移除旧标），一致的不更新，因此幂等：第二次运行时全部一致，
       更新数为 0；
    3. 词法库（chunks 表）与向量库（chunk_vectors 表）分别直接
       UPDATE metadata_json 列，单事务提交；向量库文件不存在则跳过
       （只有词法库时同样可用）。不重算向量 BLOB——metadata 变更不
       影响向量，直接 UPDATE 即可，也无需 embedding provider（不联网、
       无维度依赖）；
    4. 打印每本书统计 `{source}: 标记 N / 更新 M 个 chunk`（N = 本次
       分类为 frontmatter 的 chunk 数，M = 实际更新的行数）。

    返回 0 表示成功；阻塞书跳过不报错（与入库/verify 的 blocked 语义
    一致）。
    """
    index = SqliteKnowledgeIndex(lexical_db)
    changed_total = 0
    try:
        for book in books:
            if book.blocked:
                print(f"    跳过（阻塞：{book.blocked}）")
                continue
            chunks = index.chunks_of_document(book.source)
            if not chunks:
                print(f"{book.source}: 无已入库 chunk（跳过）")
                continue
            updates: list[tuple[str, str]] = []
            marked = 0
            for chunk in chunks:
                is_fm = classify_frontmatter(chunk.content, chunk.page)
                new_metadata = dict(chunk.metadata)
                if is_fm:
                    marked += 1
                    if new_metadata.get("chunk_class") == "frontmatter":
                        continue  # 已一致，无需更新
                    new_metadata["chunk_class"] = "frontmatter"
                elif new_metadata.get("chunk_class") == "frontmatter":
                    # 旧标记被新分类推翻：移除（与分类结果双向一致）。
                    new_metadata.pop("chunk_class", None)
                else:
                    continue
                updates.append(
                    (
                        chunk.chunk_id,
                        json.dumps(new_metadata, ensure_ascii=False),
                    )
                )
            if updates:
                _update_metadata_json(lexical_db, "chunks", updates)
                if vector_db.exists():
                    _update_metadata_json(vector_db, "chunk_vectors", updates)
                changed_total += len(updates)
            print(f"{book.source}: 标记 {marked} / 更新 {len(updates)} 个 chunk")
        if changed_total:
            print(f"重标注完成: 共更新 {changed_total} 个 chunk（幂等，可重复运行）")
        else:
            print("重标注完成: 无变化（所有 chunk 已一致）")
        return 0
    finally:
        index.close()


# ── 命令行入口 ───────────────────────────────────────────────────


def _make_provider(name: str) -> EmbeddingProvider:
    """按 --provider 名称构造 embedding provider（hash 默认 | fastembed 真实语义）。

    - hash（默认）：内置字符哈希替身，256 维，离线零依赖，语义能力
      有限（降级方案，见 embedding.py 模块注释）；
    - fastembed：真实语义模型 BAAI/bge-small-zh-v1.5，512 维；首次
      构造会联网下载模型（约 100MB，一次性），之后完全离线缓存。
    两者维度不同：更换 provider 后旧向量库打开时维度校验失败，需
    删除向量库文件（或改用新的 --vector-db 路径）后重新入库重建——
    --force 无法绕过维度守卫（详见 embedding.py 与选型文档）。
    """
    if name == "fastembed":
        return FastEmbedProvider()
    return HashEmbeddingProvider()


def _print_progress(page: int, total: int) -> None:
    """解析进度回调：每 _PROGRESS_EVERY 页打印一次，最后一页必打印。"""
    if page % _PROGRESS_EVERY == 0 or page == total:
        print(f"    解析进度: {page}/{total} 页", flush=True)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。退出码：0 成功（verify 全过）/ 1 有书失败或用例失败 / 2 参数或清单错误。"""
    parser = argparse.ArgumentParser(
        description="批量解析 data/books/ 教材并入库（SQLite 持久化，幂等可续跑）"
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help="知识源清单 JSON 路径"
    )
    parser.add_argument(
        "--books-dir",
        type=Path,
        default=DEFAULT_BOOKS_DIR,
        help="PDF 所在目录（默认 data/books）",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite 知识库文件路径"
    )
    parser.add_argument(
        "--books",
        default=None,
        help="逗号分隔的 source 子集（如 ml-lihang,dl-d2l）；默认全部书",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新入库（忽略完成标记，先清标记再入库）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="只运行检索用例验证（不写入，--books 子集仍生效）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="验证时每个用例取前 N 个检索结果（1-10）",
    )
    parser.add_argument(
        "--chunking",
        choices=["character", "semantic"],
        default="character",
        help="分块策略（默认 character，与 S3-T1 一致）：character=字符窗口；"
        "semantic=按章节标题/段落边界，并保护公式与代码块不被截断。"
        "注意：完成标记不记录策略，更换策略后需加 --force 重新入库",
    )
    parser.add_argument(
        "--vector",
        action="store_true",
        help="同步构建向量索引（S3-T4）：每个分块额外写入向量库，检索可命中"
        "同义表述。默认 Embedding 用内置哈希替身（离线零依赖，语义能力有限，"
        "选型与真实语义模型接入方式见 docs/EMBEDDING_SELECTION.md）。"
        "词法库已入库而向量库缺失时（先前未带 --vector 入库），"
        "本开关会自动从词法库补建向量，无需 --force 重新解析",
    )
    parser.add_argument(
        "--provider",
        choices=["hash", "fastembed"],
        default="hash",
        help="向量 embedding 提供方（默认 hash，离线零依赖）：hash=内置"
        "字符哈希替身（256 维，语义能力有限，降级方案）；fastembed=真实"
        "语义模型 BAAI/bge-small-zh-v1.5（512 维，首次使用联网下载模型"
        "约 100MB，之后完全离线）。注意：两者维度不同，更换后旧向量库"
        "不匹配：需删除向量库文件（或改用新的 --vector-db 路径）后重建"
        "（--force 无法绕过维度守卫）",
    )
    parser.add_argument(
        "--vector-db",
        type=Path,
        default=DEFAULT_VECTOR_DB_PATH,
        help="向量索引数据库路径（默认 data/vector_knowledge.db，随 data/ 不进 git）",
    )
    parser.add_argument(
        "--relabel-frontmatter",
        action="store_true",
        help="H-T2：只对已入库 chunk 重新执行前言/目录分类并更新两库 "
        "metadata_json 列，不重解析 PDF、不重算向量、幂等；--books 子集 "
        "与 --db/--vector-db 生效",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.top_k <= 10:
        print("错误: --top-k 必须在 1-10 之间", file=sys.stderr)
        return 2

    try:
        manifest = load_manifest(args.manifest, books_dir=args.books_dir)
        books = select_books(manifest, args.books)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    index = SqliteKnowledgeIndex(args.db)
    # S3-T4：向量库在「入库/补建」路径（--vector）显式打开；
    # --verify 分支用 open_vector_index_if_available「存在才打开」——
    # 文件不存在时返回 None（不会白白创建空库文件），检索自动降级
    # 为词法单路（S3-T5 混合检索的降级语义，见 hybrid.py 模块注释）。
    # embedding provider 由 --provider 参数选择（默认 hash 内置哈希
    # 替身；fastembed 为真实语义模型，差异见 _make_provider）。
    # 构造时机：只有真正需要向量路时才构造——fastembed 首次构造会
    # 联网下载模型（约 100MB），--verify 时向量库文件不存在则检索
    # 注定降级为纯词法，此时构造只会白白触发下载。
    # 更换 provider 后维度可能不同（hash=256 / fastembed=512），旧
    # 向量库打开时会维度校验失败：--vector 路径显式报错并提示删除
    # 向量库文件/改用新 --vector-db（--force 无法绕过——维度守卫在
    # 打开旧库时触发，早于清完成标记），--verify 路径由
    # open_vector_index_if_available 捕获并降级为纯词法（不报错），
    # 详见 embedding.py 与选型文档。
    need_provider = args.vector or (args.verify and args.vector_db.exists())
    provider: EmbeddingProvider | None = None
    try:
        if need_provider:
            provider = _make_provider(args.provider)
    except RuntimeError as exc:
        # fastembed 未安装或模型下载失败：显式报错而不是静默退回哈希
        # ——用户显式选了 fastembed，静默换 provider 会与库维度错位。
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    vector_index: SqliteVectorKnowledgeIndex | None = None
    try:
        if args.verify:
            # S3-T5：混合检索是 --verify 的默认路径——词法库必开，
            # 向量库「文件存在才打开」（从未 --vector 入库则文件不存在，
            # 自动降级为纯词法，不抛错，行为与 S3-T1 完全一致）；打开
            # 失败（如向量库维度与当前 provider 不匹配——换过 provider
            # 未重建）同样降级为纯词法，详见 hybrid.py 的
            # open_vector_index_if_available。provider 透传 --provider
            # 的选择：旧维度库打不开时降级，不阻断验证（verify 允许降级）。
            vector_index = open_vector_index_if_available(
                args.vector_db, provider=provider
            )
            service = KnowledgeService(HybridKnowledgeIndex(index, vector_index))
            # 阻塞书（数据源不可用）不参与验证：其用例保留在清单里，
            # 待 blocked 标记移除后自动恢复验证。
            blocked_books = [book for book in books if book.blocked]
            active_books = [book for book in books if not book.blocked]
            cases = [case for book in active_books for case in book.verify]
            if not cases:
                print("没有可验证的检索用例（所有书均被阻塞或清单为空）。")
                return 0
            results = verify_cases(service, cases, top_k=args.top_k)
            failed = 0
            for result in results:
                detail = "、".join(
                    f"{source}({score:g})" for source, score in result.top_hits
                ) or "无命中"
                verdict = "PASS" if result.passed else "FAIL"
                print(
                    f"[{verdict}] 期望 {result.expected_source}｜查询「{result.query}」"
                    f"｜前 {args.top_k} 名: {detail}"
                )
                failed += 0 if result.passed else 1
            if blocked_books:
                blocked_sources = "、".join(book.source for book in blocked_books)
                print(
                    f"跳过 {len(blocked_books)} 本阻塞书（清单 blocked 字段）："
                    f"{blocked_sources}"
                )
            print(
                f"验证完成: {len(results) - failed}/{len(results)} 用例通过"
                f"（另有 {len(blocked_books)} 本阻塞书未验证）"
            )
            return 0 if failed == 0 else 1

        if args.relabel_frontmatter:
            # H-T2 增量重标注分支：只更新两库 metadata_json 列（不重解析
            # PDF、不重算向量、不需要 embedding provider，幂等），处理完
            # 直接返回，不进入入库流程（见 relabel_frontmatter）。
            return relabel_frontmatter(args.db, args.vector_db, books)

        # --vector：走到这里说明不是 --verify 分支（verify 已在上方
        # return），此时才打开向量库，避免空库文件被创建。provider
        # 由 --provider 选择（hash 默认；fastembed 为真实语义模型，
        # 512 维——更换 provider 后旧库维度不匹配，打开时报错：需删除
        # 向量库文件（或改用新的 --vector-db 路径）后重建，--force 无法
        # 绕过维度守卫，见 vector_index 的维度守卫）。
        if args.vector:
            try:
                vector_index = SqliteVectorKnowledgeIndex(
                    args.vector_db,
                    provider=provider if provider is not None else HashEmbeddingProvider(),
                )
            except (ValueError, sqlite3.Error, OSError) as exc:
                # 维度守卫/库损坏发生在打开旧库的构造期（_load_all），
                # 早于 --force 清完成标记——因此 --force 无法绕过：必须
                # 删除旧向量库文件或改用新的 --vector-db 路径重建。
                # 注意：构造失败时 __init__ 已自行关闭连接（防泄漏），
                # 此处 vector_index 仍为 None，finally 不会重复 close。
                print(
                    f"错误: 向量库维度与所选 provider 不匹配或库损坏: {exc}\n"
                    "请删除向量库文件（或改用新的 --vector-db 路径）后重新运行；"
                    "--force 无法绕过维度守卫",
                    file=sys.stderr,
                )
                return 2

        failed_books: list[str] = []
        blocked_count = 0
        total_books = len(books)
        for book_number, book in enumerate(books, start=1):
            pdf_path = args.books_dir / book.file
            print(f"[{book_number}/{total_books}] 《{book.title}》 ({book.source})")
            try:
                result = ingest_book(
                    index,
                    book,
                    pdf_path,
                    force=args.force,
                    progress=_print_progress,
                    chunking=args.chunking,
                    vector_index=vector_index,
                )
            except ValueError as exc:
                # 单本书失败只影响它自己：无完成标记，下次默认运行会自动重试；
                # 其余书继续处理（失败续跑能力）。
                print(f"    [失败] {exc}", file=sys.stderr)
                failed_books.append(book.source)
                continue
            if result.status == "blocked":
                # 阻塞书（数据源不可用，如扫描版无文本层）：跳过计入成功，
                # 待 blocked 标记移除后自动恢复入库。
                blocked_count += 1
                print(
                    f"    跳过（阻塞：{book.blocked}）；"
                    "待数据源可用并移除 blocked 标记后自动恢复入库"
                )
            elif result.status == "skipped":
                print("    已入库（完成标记存在），跳过；--force 可强制重入库")
            else:
                print(
                    f"    入库完成: {result.pages} 页 → {result.chunks} 个分块"
                )

        if failed_books:
            print(
                f"有 {len(failed_books)} 本书入库失败: {', '.join(failed_books)}；"
                "修复后重新运行即可续跑（失败的书无完成标记会自动重试）",
                file=sys.stderr,
            )
            return 1
        blocked_note = f"（其中 {blocked_count} 本因阻塞跳过）" if blocked_count else ""
        print(
            f"全部 {total_books} 本书处理完成{blocked_note}；"
            "可运行 --verify 验证检索用例命中。"
        )
        return 0
    finally:
        index.close()
        if vector_index is not None:
            vector_index.close()


if __name__ == "__main__":
    sys.exit(main())
