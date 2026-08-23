"""D6-T3 知识库检索 REST 端点测试:POST /knowledge/search。

覆盖范围:
- 空库返回 200 + 空 hits(不报错,与 core 语义一致);
- 注入文档后可检索:命中返回逻辑 source 引用与截断摘要;
- 路径不泄漏:文档内容含文件系统路径时,响应不出现原始路径(回归);
- top_k 越界 / query 空、空白、超长 → 422(由 Pydantic Field 拦截,
  不依赖 core 的 ValueError 兜底);
- service 缺失(app.state 未装配)→ 503 + knowledge_unavailable;
- service.search 内部异常 → 500 + internal_error,不泄底层细节;
- OpenAPI 契约:三个模型入 schema,top_k 的 ge/le 边界可见;
- lifespan 装配:app.state.knowledge_service 挂载/清理,真实检索可用。

D6-T5 文档管理端点覆盖范围(POST/GET/DELETE /knowledge/documents):
- 上传 txt/pdf 成功:201 + 文档元数据(document_id/source/page_count/
  chunk_count),入库后检索真实命中(替代「列表可查到」的验证——core
  无文档枚举能力,GET 列表恒空,见 list_documents 路由注释);
- 同名重传幂等替换:同 document_id 上传两次,新内容命中、旧内容清除;
- 校验拦截:扩展名白名单 / 大小上限(monkeypatch 小上限)/ 空文件 /
  损坏 PDF → 422 invalid_request,均在 API 层拦截;
- 删除:204 无 body、检索无残留;删除不存在的文档幂等 204(不 404,
  core 删除语义无存在性判断);
- 故障路径:service 缺失 503;入库异常 500 不泄细节;
- OpenAPI:新契约模型与三个路径可见。

检索测试用 InMemoryKnowledgeIndex(与 test_knowledge_* 的 core 层
测试同一构造方式),不依赖真实 data/ 目录与 embedding 模型。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pytest import MonkeyPatch

from api.app import create_app
from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument, SearchHit
from core.knowledge.service import KnowledgeService

# 与 api/knowledge.py 的 SUMMARY_MAX_LENGTH 同口径:摘要截断上限。
SUMMARY_MAX_LENGTH = 200

_LONG_CONTENT = "一元二次方程是初中数学的核心内容。" + "辅助材料。" * 60


def _make_service(content: str | None = None) -> KnowledgeService:
    """构造真实 KnowledgeService(InMemory 词法索引),可选注入一篇文档。"""
    service = KnowledgeService(InMemoryKnowledgeIndex())
    if content is not None:
        service.add_documents(
            [
                KnowledgeDocument(
                    document_id="guide",
                    content=content,
                    source="guide.txt",
                )
            ]
        )
    return service


async def _post(app: FastAPI, payload: dict[str, object]) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/knowledge/search", json=payload)


def _app_with_service(service: KnowledgeService) -> FastAPI:
    """构造 app 并手工注入 service(不跑 lifespan,避免图装配开销)。"""
    app = create_app()
    app.state.knowledge_service = service
    return app


# ── D6-T5 上传/管理辅助 ────────────────────────────────────────────


def _make_pdf(page_texts: list[str]) -> bytes:
    """构造可被 pypdf 解析的最小 PDF(与 test_knowledge_loaders 的
    _write_pdf 同一结构,这里返回 bytes 供上传)。

    说明:pypdf 提取中文字体文本依赖字体子集,测试用 latin-1 可编码
    的英文文本保证提取确定性。
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
    return bytes(pdf)


async def _upload(
    app: FastAPI,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/knowledge/documents",
            files={"file": (filename, content, content_type)},
        )


async def _get_documents(app: FastAPI) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/knowledge/documents")


async def _delete_document(app: FastAPI, document_id: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.delete(f"/knowledge/documents/{document_id}")


# ── 正常检索路径 ───────────────────────────────────────────────────


def test_search_returns_empty_hits_for_empty_index() -> None:
    app = _app_with_service(_make_service())

    response = asyncio.run(_post(app, {"query": "任何内容", "top_k": 5}))

    assert response.status_code == 200
    assert response.json() == {"hits": []}


def test_search_returns_hit_with_logical_citation_and_truncated_summary() -> None:
    app = _app_with_service(_make_service(_LONG_CONTENT))

    response = asyncio.run(_post(app, {"query": "一元二次方程", "top_k": 3}))

    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 1
    hit = hits[0]
    # summary 截断到 200 字符 + 省略号,不返回 chunk 全文。
    assert hit["summary"] == _LONG_CONTENT[:SUMMARY_MAX_LENGTH] + "…"
    assert len(hit["summary"]) == SUMMARY_MAX_LENGTH + 1
    # citation 是逻辑 source 标识,不含任何文件系统路径。
    citation = hit["citation"]
    assert citation["document_id"] == "guide"
    assert citation["source"] == "guide.txt"
    assert citation["page"] is None
    assert isinstance(citation["chunk_id"], str)
    assert hit["score"] >= 0


def test_search_does_not_leak_filesystem_paths(tmp_path: Path) -> None:
    """文档内容含文件系统路径时,响应不得出现原始路径(回归)。

    路径进入的是 chunk 内容(不是 source——source 由 core 强制为
    逻辑标识),泄漏风险点在响应 JSON:路径中的反斜杠会被 JSON
    转义为 \\,因此「单反斜杠的原始路径字符串」绝不允许出现在序列化
    文本里。用显式 Windows 风格路径(含反斜杠,任何平台都触发 JSON
    转义,测试跨平台稳定);同时用 tmp_path.name 断言路径确实进入了
    命中摘要,保证本测试不是「内容根本没被检索」的假阳性。
    """
    secret = f"C:\\Users\\runner\\temp\\{tmp_path.name}"
    content = f"机密档案缓存位置:{secret}"
    app = _app_with_service(_make_service(content))

    response = asyncio.run(_post(app, {"query": "机密档案", "top_k": 3}))

    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 1
    # 路径确实进入了被检索的 chunk(测试有效性)。
    assert tmp_path.name in hits[0]["summary"]
    # 原始路径(单反斜杠形式)绝不出现在响应文本中。
    assert secret not in response.text


# ── 请求校验(422 由 Pydantic Field 拦截)───────────────────────────


def test_search_rejects_top_k_out_of_range() -> None:
    app = _app_with_service(_make_service())

    for top_k in (0, -1, 11):
        response = asyncio.run(_post(app, {"query": "测试", "top_k": top_k}))

        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["error_code"] == "invalid_request"


def test_search_rejects_blank_or_too_long_query() -> None:
    app = _app_with_service(_make_service())

    for query in ("", "   ", "长" * 501):
        response = asyncio.run(_post(app, {"query": query, "top_k": 5}))

        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["error_code"] == "invalid_request"


# ── 装配与故障路径 ─────────────────────────────────────────────────


def test_search_returns_503_when_service_missing() -> None:
    """app.state 无 knowledge_service(lifespan 未跑)→ 503 稳定错误码。"""
    app = create_app()

    response = asyncio.run(_post(app, {"query": "测试", "top_k": 5}))

    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["error_code"] == "knowledge_unavailable"


class _BrokenService:
    """测试替身:search 抛异常,模拟索引损坏等运行时故障。"""

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        raise RuntimeError("index corrupted: 内部细节")


def test_search_maps_index_failure_to_500_without_leaking_details() -> None:
    app = create_app()
    app.state.knowledge_service = _BrokenService()

    response = asyncio.run(_post(app, {"query": "测试", "top_k": 5}))

    assert response.status_code == 500
    body = response.json()
    assert body["detail"]["error_code"] == "internal_error"
    # 不向客户端暴露底层异常原文。
    assert "corrupted" not in response.text


# ── OpenAPI 契约 ───────────────────────────────────────────────────


def test_openapi_exposes_knowledge_search_contracts() -> None:
    app = create_app()
    schemas = app.openapi()["components"]["schemas"]

    request_schema = schemas["KnowledgeSearchRequest"]
    query_schema = request_schema["properties"]["query"]
    assert query_schema["minLength"] == 1
    assert query_schema["maxLength"] == 500
    assert query_schema["type"] == "string"
    top_k = request_schema["properties"]["top_k"]
    assert top_k["minimum"] == 1
    assert top_k["maximum"] == 10
    assert top_k["default"] == 5

    response_schema = schemas["KnowledgeSearchResponse"]
    assert response_schema["properties"]["hits"]["type"] == "array"

    hit_schema = schemas["SearchHitDto"]
    assert set(hit_schema["properties"]) == {"summary", "citation", "score"}


# ── lifespan 装配集成 ──────────────────────────────────────────────


def test_lifespan_mounts_knowledge_service_and_search_works(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """真实装配:lifespan 挂载 service 后可检索,退出后清空。

    与 test_api_knowledge_wiring 同一自包含模式:词法库指向 tmp、
    向量库缺失自动降级、hash embedding 零依赖,不碰真实 data/。
    """
    knowledge_db = tmp_path / "knowledge.db"
    index = SqliteKnowledgeIndex(knowledge_db)
    index.upsert(
        [
            KnowledgeChunk(
                chunk_id="algebra-1",
                document_id="algebra",
                content="一元二次方程可以使用求根公式求解。",
                source="algebra",
                page=1,
                start=0,
                end=20,
            )
        ]
    )
    index.close()

    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    monkeypatch.setenv("API_KNOWLEDGE_DB_PATH", str(knowledge_db))
    monkeypatch.setenv("API_VECTOR_DB_PATH", str(tmp_path / "missing-vector.db"))
    monkeypatch.setenv("API_KNOWLEDGE_EMBEDDING", "hash")
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            service = getattr(app.state, "knowledge_service", None)
            assert isinstance(service, KnowledgeService)
            response = await _post(app, {"query": "一元二次方程", "top_k": 3})
            assert response.status_code == 200
            hits = response.json()["hits"]
            assert len(hits) == 1
            assert hits[0]["citation"]["source"] == "algebra"

    asyncio.run(verify_runtime())

    # lifespan 退出后清空引用(与 graph/session_store 同一清理语义)。
    assert getattr(app.state, "knowledge_service", None) is None


# ── D6-T5 上传与管理端点 ───────────────────────────────────────────


def test_upload_txt_returns_metadata_and_is_searchable() -> None:
    app = _app_with_service(_make_service())
    content = "一元二次方程求根公式:x = (-b ± sqrt(b^2 - 4ac)) / 2a。"

    response = asyncio.run(_upload(app, "guide.txt", content.encode("utf-8")))

    assert response.status_code == 201
    body = response.json()
    assert body["document_id"] == "guide"
    assert body["source"] == "guide.txt"
    assert body["page_count"] is None
    assert body["chunk_count"] == 1

    # 文档清单:API 层注册表记录上传(D6-T5 review 修正——core 无枚举
    # 能力,注册表满足「上传后可从列表查到」验收)。
    listing = asyncio.run(_get_documents(app))
    assert listing.status_code == 200
    documents = listing.json()["documents"]
    assert len(documents) == 1
    assert documents[0]["document_id"] == "guide"
    assert documents[0]["source"] == "guide.txt"
    assert documents[0]["chunk_count"] == 1

    # 入库真实生效:检索可命中上传内容(「列表可查到」的替代验证)。
    search = asyncio.run(_post(app, {"query": "一元二次方程", "top_k": 3}))
    assert search.status_code == 200
    hits = search.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["citation"]["document_id"] == "guide"
    assert hits[0]["citation"]["source"] == "guide.txt"
    assert hits[0]["citation"]["page"] is None


def test_upload_strips_path_prefix_from_filename() -> None:
    """浏览器式假路径文件名不泄漏:source 只取纯文件名(防御回归)。"""
    app = _app_with_service(_make_service())

    response = asyncio.run(
        _upload(app, r"C:\fakepath\guide.txt", "内容".encode())
    )

    assert response.status_code == 201
    body = response.json()
    assert body["document_id"] == "guide"
    assert body["source"] == "guide.txt"
    assert "fakepath" not in response.text


def test_upload_pdf_returns_page_and_chunk_counts() -> None:
    app = _app_with_service(_make_service())
    pdf = _make_pdf(["First page content", "Second page content"])

    response = asyncio.run(_upload(app, "book.pdf", pdf))

    assert response.status_code == 201
    body = response.json()
    assert body["document_id"] == "book"
    assert body["source"] == "book.pdf"
    assert body["page_count"] == 2
    assert body["chunk_count"] == 2

    # 两页都真实入库,可分别按页检索。
    first = asyncio.run(_post(app, {"query": "First page", "top_k": 3}))
    assert first.json()["hits"][0]["citation"]["page"] == 1
    second = asyncio.run(_post(app, {"query": "Second page", "top_k": 3}))
    assert second.json()["hits"][0]["citation"]["page"] == 2


def test_upload_same_filename_replaces_previous_content() -> None:
    """同名重传 → 同一 document_id → core 替换语义,新内容覆盖旧内容。"""
    app = _app_with_service(_make_service())

    first = asyncio.run(_upload(app, "guide.txt", "旧版:一元二次方程".encode()))
    assert first.status_code == 201

    second = asyncio.run(_upload(app, "guide.txt", "新版:牛顿第一定律".encode()))
    assert second.status_code == 201
    assert second.json()["document_id"] == "guide"

    fresh = asyncio.run(_post(app, {"query": "牛顿第一定律", "top_k": 3}))
    assert fresh.json()["hits"][0]["citation"]["source"] == "guide.txt"
    # 旧 chunk 已删除:查旧内容独有且与新内容零单字重叠的词
    # ("二次方程" 的字与新内容"新版:牛顿第一定律"无重叠;若用"旧版"
    # 会因单字"版"重叠产生低分命中)。
    stale = asyncio.run(_post(app, {"query": "二次方程", "top_k": 3}))
    assert stale.json()["hits"] == []


def test_upload_rejects_disallowed_extensions() -> None:
    app = _app_with_service(_make_service())

    for filename in ("notes.docx", "virus.exe", "noext"):
        response = asyncio.run(_upload(app, filename, b"whatever"))

        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "invalid_request"


def test_upload_rejects_oversized_file(monkeypatch: MonkeyPatch) -> None:
    """大小上限用 monkeypatch 调小,避免在测试里构造 10MB 内存。"""
    monkeypatch.setattr("api.knowledge.MAX_UPLOAD_BYTES", 1024)
    app = _app_with_service(_make_service())

    too_big = asyncio.run(_upload(app, "big.txt", b"x" * 2048))
    assert too_big.status_code == 422
    assert too_big.json()["detail"]["error_code"] == "invalid_request"

    # 恰好等于上限不误杀。
    at_limit = asyncio.run(_upload(app, "ok.txt", b"y" * 1024))
    assert at_limit.status_code == 201


def test_upload_rejects_empty_and_unparseable_files() -> None:
    """空文件 / 损坏 PDF 均在 API 层映射 422(内容不可解析属请求问题)。"""
    app = _app_with_service(_make_service())

    empty = asyncio.run(_upload(app, "empty.txt", b""))
    assert empty.status_code == 422
    assert empty.json()["detail"]["error_code"] == "invalid_request"

    broken_pdf = asyncio.run(_upload(app, "broken.pdf", b"not a pdf"))
    assert broken_pdf.status_code == 422
    assert broken_pdf.json()["detail"]["error_code"] == "invalid_request"


def test_delete_document_removes_chunks_and_is_idempotent() -> None:
    app = _app_with_service(_make_service())
    upload = asyncio.run(_upload(app, "guide.txt", "待删除内容".encode()))
    document_id = upload.json()["document_id"]

    deleted = asyncio.run(_delete_document(app, document_id))
    assert deleted.status_code == 204
    assert deleted.content == b""

    # 删除后检索无残留。
    search = asyncio.run(_post(app, {"query": "待删除内容", "top_k": 3}))
    assert search.json()["hits"] == []

    # 删除后注册表同步移除:列表不再包含该文档。
    listing = asyncio.run(_get_documents(app))
    assert listing.json()["documents"] == []

    # 幂等:再次删除不存在的文档仍 204(core 删除语义,见路由注释)。
    again = asyncio.run(_delete_document(app, document_id))
    assert again.status_code == 204


def test_upload_returns_503_when_service_missing() -> None:
    """依赖复用:app.state 无 knowledge_service → 上传也走 503 稳定码。"""
    app = create_app()

    response = asyncio.run(_upload(app, "guide.txt", "内容".encode()))

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "knowledge_unavailable"


class _BrokenUploadService:
    """测试替身:入库阶段抛异常,模拟存储故障(鸭子契约只需 search 可调用)。"""

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: dict[str, str] | None = None,
    ) -> list[SearchHit]:
        return []

    def add_documents(
        self, documents: Iterable[KnowledgeDocument]
    ) -> list[KnowledgeChunk]:
        raise RuntimeError("storage corrupted: 内部细节")


def test_upload_maps_storage_failure_to_500_without_leaking_details() -> None:
    app = create_app()
    app.state.knowledge_service = _BrokenUploadService()

    response = asyncio.run(_upload(app, "guide.txt", "内容".encode()))

    assert response.status_code == 500
    body = response.json()
    assert body["detail"]["error_code"] == "internal_error"
    # 不向客户端暴露底层异常原文。
    assert "corrupted" not in response.text


def test_openapi_exposes_document_upload_contracts() -> None:
    app = create_app()
    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]

    upload_schema = schemas["KnowledgeDocumentUploadResponse"]
    assert set(upload_schema["properties"]) == {
        "document_id",
        "source",
        "page_count",
        "chunk_count",
        # S5-C3 上传语义补齐：同名文件替换标志。
        "replaced",
    }
    list_schema = schemas["KnowledgeDocumentListResponse"]
    assert list_schema["properties"]["documents"]["type"] == "array"
    # 未直接用于路由的列表条目模型也经契约通道进入 schema。
    assert "KnowledgeDocumentListEntry" in schemas

    paths = openapi["paths"]
    assert set(paths["/knowledge/documents"]) == {"post", "get"}
    assert "delete" in paths["/knowledge/documents/{document_id}"]


# ── S5-C3 上传语义补齐：replaced 标志 + 完成标记 ───────────────────


def test_upload_same_file_twice_reports_replaced_and_writes_mark(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """同名文件上传两次：第二次 replaced=true；完成标记使 catalog 有入库时间。"""
    from core.knowledge.catalog import SqliteKnowledgeCatalog

    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    knowledge_db = tmp_path / "knowledge.db"
    monkeypatch.setenv("API_KNOWLEDGE_DB_PATH", str(knowledge_db))
    monkeypatch.setenv("API_VECTOR_DB_PATH", str(tmp_path / "missing-vector.db"))
    monkeypatch.setenv("API_KNOWLEDGE_EMBEDDING", "hash")
    app = create_app()
    content = "一元二次方程求根公式与判别式说明。"

    async def scenario() -> tuple[Response, Response]:
        async with app.router.lifespan_context(app):
            first = await _upload(app, "guide.txt", content.encode("utf-8"))
            second = await _upload(app, "guide.txt", content.encode("utf-8"))
            return first, second

    first, second = asyncio.run(scenario())

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["replaced"] is False
    assert second.json()["replaced"] is True

    catalog = SqliteKnowledgeCatalog(knowledge_db)
    try:
        # C1 修复后 catalog 按 document_id 分组：public 上传为裸 stem。
        docs = {d.document_id: d for d in catalog.list_documents()}
        info = docs.get("guide")
        assert info is not None
        assert info.ingested_at is not None
    finally:
        catalog.close()


def test_upload_first_time_reports_replaced_false(tmp_path: Path) -> None:
    """首次上传 replaced=false（无注册表/catalog 前身时的新建语义）。"""
    app = _app_with_service(_make_service())
    response = asyncio.run(_upload(app, "notes.txt", "全新笔记内容".encode()))

    assert response.status_code == 201
    assert response.json()["replaced"] is False


# ── S5-C1 知识空间：命名空间上传/聚合/scope 贯通 ────────────────────


def test_upload_with_namespace_prefixes_id_and_namespaces_aggregate(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """非 public 空间上传 → document_id 带前缀；同名重传 replaced=true；
    /knowledge/namespaces 聚合 public 与 course-a 各 1 篇。"""

    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    knowledge_db = tmp_path / "knowledge.db"
    monkeypatch.setenv("API_KNOWLEDGE_DB_PATH", str(knowledge_db))
    monkeypatch.setenv("API_VECTOR_DB_PATH", str(tmp_path / "missing-vector.db"))
    monkeypatch.setenv("API_KNOWLEDGE_EMBEDDING", "hash")
    app = create_app()
    content = "一元二次方程求根公式与判别式说明。"

    async def scenario() -> tuple[Response, Response, Response, Response]:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                public_doc = await client.post(
                    "/knowledge/documents",
                    files={"file": ("pub.txt", content.encode("utf-8"), "text/plain")},
                )
                first = await client.post(
                    "/knowledge/documents",
                    files={
                        "file": ("guide.txt", content.encode("utf-8"), "text/plain")
                    },
                    data={"knowledge_namespace": "course-a"},
                )
                second = await client.post(
                    "/knowledge/documents",
                    files={
                        "file": ("guide.txt", content.encode("utf-8"), "text/plain")
                    },
                    data={"knowledge_namespace": "course-a"},
                )
                namespaces = await client.get("/knowledge/namespaces")
                return public_doc, first, second, namespaces

    public_doc, first, second, namespaces = asyncio.run(scenario())

    assert public_doc.status_code == 201
    # 默认归入 public：裸 ID（决策 7——public 空间不加前缀）。
    assert public_doc.json()["document_id"] == "pub"
    # 非 public 空间 → document_id 前缀化，同名重传 replaced=true。
    assert first.status_code == 201
    assert first.json()["document_id"] == "course-a:guide"
    assert second.status_code == 201
    assert second.json()["replaced"] is True
    # 空间聚合：public 与 course-a 各 1 篇（course-a 同名替换后仍 1）。
    aggregated = {
        item["namespace"]: item["document_count"]
        for item in namespaces.json()["namespaces"]
    }
    assert aggregated["public"] == 1
    assert aggregated["course-a"] == 1


def test_upload_rejects_invalid_knowledge_namespace_format() -> None:
    """空间标识校验对齐 manifest 的 source 规则（连字符边界）。

    - "course-" 尾连字符 / "a--b" 连续连字符 → 422；
    - "course-a" 合法形态照常通过。
    """
    app = create_app()
    content = "正文内容。"

    async def scenario() -> list[Response]:
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                responses = []
                for bad in ("course-", "a--b"):
                    responses.append(
                        await client.post(
                            "/knowledge/documents",
                            files={
                                "file": (
                                    f"{bad}.txt",
                                    content.encode("utf-8"),
                                    "text/plain",
                                )
                            },
                            data={"knowledge_namespace": bad},
                        )
                    )
                ok = await client.post(
                    "/knowledge/documents",
                    files={
                        "file": ("guide.txt", content.encode("utf-8"), "text/plain")
                    },
                    data={"knowledge_namespace": "course-a"},
                )
                return [*responses, ok]

    responses = asyncio.run(scenario())

    for response in responses[:2]:
        assert response.status_code == 422
        assert response.json()["detail"]["error_code"] == "invalid_request"
    hyphen_ok = responses[2]
    assert hyphen_ok.status_code == 201
    assert hyphen_ok.json()["document_id"] == "course-a:guide"


def test_namespaces_endpoint_empty_catalog_returns_empty_list(tmp_path: Path) -> None:
    """空库（catalog 无表）→ 空列表而非报错（与「空库检索返回空」一致）。"""
    from core.knowledge.catalog import SqliteKnowledgeCatalog

    app = create_app()
    catalog = SqliteKnowledgeCatalog(tmp_path / "empty-knowledge.db")
    app.state.knowledge_catalog = catalog
    try:
        response = asyncio.run(_get_namespaces(app))
    finally:
        catalog.close()

    assert response.status_code == 200
    assert response.json() == {"namespaces": []}


async def _get_namespaces_async(app: FastAPI) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/knowledge/namespaces")


def _get_namespaces(app: FastAPI) -> Response:
    return asyncio.run(_get_namespaces_async(app))


# ── S5-C1 决策 4：scope 贯通（knowledge_scope → service.namespace）──


class _NamespaceSpyService:
    """记录 search 收到的 namespace 参数并返回空结果（工具走 search 路）。"""

    def __init__(self) -> None:
        self.namespaces: list[str | None] = []

    def search(
        self,
        query: str,
        top_k: int = 5,
        metadata_filter: dict[str, str] | None = None,
        namespace: str | None = None,
    ) -> list[SearchHit]:
        self.namespaces.append(namespace)
        return []


def test_search_knowledge_threads_public_namespace_from_scope() -> None:
    """未绑定会话（上游解析为 "public"）→ 工具把 "public" 透传给 service。"""
    from core.knowledge.tools import create_search_knowledge_tool, knowledge_scope

    spy = _NamespaceSpyService()
    tool = create_search_knowledge_tool(spy)  # type: ignore[arg-type]
    token = knowledge_scope.set("public")
    try:
        tool.invoke({"query": "任何内容"})
    finally:
        knowledge_scope.reset(token)

    assert spy.namespaces == ["public"]


def test_search_knowledge_threads_bound_namespace_from_scope() -> None:
    """绑定 course-a 的会话 → 工具把 "course-a" 透传给 service。"""
    from core.knowledge.tools import create_search_knowledge_tool, knowledge_scope

    spy = _NamespaceSpyService()
    tool = create_search_knowledge_tool(spy)  # type: ignore[arg-type]
    token = knowledge_scope.set("course-a")
    try:
        tool.invoke({"query": "任何内容"})
    finally:
        knowledge_scope.reset(token)

    assert spy.namespaces == ["course-a"]


# ── S5-C1/C2：命名空间上传、空间聚合与树端点（含冒号 ID 编码往返）──────


def _env_for_knowledge(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    monkeypatch.setenv("API_KNOWLEDGE_DB_PATH", str(tmp_path / "knowledge.db"))
    monkeypatch.setenv("API_VECTOR_DB_PATH", str(tmp_path / "missing-vector.db"))
    monkeypatch.setenv("API_KNOWLEDGE_EMBEDDING", "hash")


async def _upload_namespaced(
    app: FastAPI, filename: str, content: bytes, namespace: str
) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/knowledge/documents",
            files={"file": (filename, content, "application/octet-stream")},
            data={"knowledge_namespace": namespace},
        )


def test_upload_to_namespace_prefixes_id_and_aggregates(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """非 public 空间上传 → document_id 带前缀；同名重传 replaced=true；
    /knowledge/namespaces 聚合 public 与 course-a 各 1 篇。"""
    _env_for_knowledge(tmp_path, monkeypatch)
    app = create_app()
    content = "一元二次方程求根公式与判别式说明。"

    async def scenario() -> list[Response]:
        async with app.router.lifespan_context(app):
            responses = []
            # public 空间一篇 + course-a 空间两篇（第二篇验证前缀替换语义）。
            responses.append(await _upload(app, "guide.txt", content.encode("utf-8")))
            responses.append(
                await _upload_namespaced(app, "guide.txt", content.encode("utf-8"), "course-a")
            )
            responses.append(
                await _upload_namespaced(app, "guide.txt", content.encode("utf-8"), "course-a")
            )
            responses.append(await _get_documents(app))
            namespaces = await (
                ASGITransport(app=app) and _get_namespaces(app)
            )
            responses.append(namespaces)
            return responses

    first_public, first_ns, second_ns, listing, namespaces = asyncio.run(scenario())

    assert first_public.json()["replaced"] is False
    assert first_ns.json()["document_id"] == "course-a:guide"
    assert second_ns.json()["document_id"] == "course-a:guide"
    assert second_ns.json()["replaced"] is True

    docs = {d["document_id"]: d for d in listing.json()["documents"]}
    # public 裸 stem、非 public 带前缀——两种形态并存且互不合并。
    assert "guide" in docs
    assert "course-a:guide" in docs

    usage = {item["namespace"]: item["document_count"] for item in namespaces.json()["namespaces"]}
    assert usage == {"public": 1, "course-a": 1}


async def _get_namespaces(app: FastAPI) -> Response:
    from httpx import ASGITransport as _T
    from httpx import AsyncClient as _AC

    async with _AC(transport=_T(app=app), base_url="http://testserver") as client:
        return await client.get("/knowledge/namespaces")


def test_tree_endpoint_flat_and_colon_id_round_trip(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """树端点两态：上传 txt（无标题结构）→ flat；冒号 ID 经 URL 编码往返。"""
    from urllib.parse import quote

    _env_for_knowledge(tmp_path, monkeypatch)
    app = create_app()
    content = "一元二次方程求根公式与判别式说明。"

    async def scenario() -> tuple[Response, Response]:
        async with app.router.lifespan_context(app):
            await _upload_namespaced(app, "guide.txt", content.encode("utf-8"), "course-a")
            encoded = quote("course-a:guide", safe="")
            tree = await _get_tree(app, encoded)
            missing = await _get_tree(app, quote("course-a:missing", safe=""))
            return tree, missing

    tree, missing = asyncio.run(scenario())

    assert tree.status_code == 200
    body = tree.json()
    assert body["kind"] == "flat"
    assert body["document_id"] == "course-a:guide"
    # txt 上传无页概念 → flat_pages 为空列表（前端渲染「无内容」占位）。
    assert body["flat_pages"] == []

    assert missing.status_code == 200
    assert missing.json()["kind"] == "flat"
    assert missing.json()["flat_pages"] == []


async def _get_tree(app: FastAPI, encoded_document_id: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(f"/knowledge/documents/{encoded_document_id}/tree")
