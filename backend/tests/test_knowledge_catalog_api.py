"""I1+I2 知识库清单/分块端点测试：GET /knowledge/overview、chunks、documents/{id}/chunks。

覆盖：
- overview：catalog 装配后返回统计 + 文档清单；catalog 未装配
  （lifespan 未跑）→ 503 knowledge_unavailable；
- list_documents：catalog 装配后脚本入库文档可见（不再只有注册表）；
- chunks/{chunk_id}：命中返回原文 + citation；未命中 / 空白 /
  超长 → 404；超长内容截断到 CHUNK_CONTENT_MAX_LENGTH；
- documents/{id}/chunks：分页（offset/limit）、摘要截断、total、
  排序稳定；limit 越界 → 422；索引未实现 chunks_of_document →
  空列表（不报错）；
- OpenAPI：新契约模型与路径可见。

用 InMemoryKnowledgeIndex 构造 service 时索引天然实现 chunk /
chunks_of_document；catalog 用 SqliteKnowledgeCatalog（读真实词法库），
与检索 service 共用同一数据。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from api.app import create_app
from core.knowledge.catalog import SqliteKnowledgeCatalog
from core.knowledge.hybrid import HybridKnowledgeIndex
from core.knowledge.index import InMemoryKnowledgeIndex, SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument
from core.knowledge.service import KnowledgeService

SUMMARY_MAX_LENGTH = 200
CHUNK_CONTENT_MAX_LENGTH = 8 * 1024

_LONG_CHUNK = "支持向量机是机器学习中重要的监督学习模型。" + "补充内容。" * 3000


def _chunk(
    chunk_id: str,
    content: str,
    *,
    document_id: str = "ml",
    page: int | None = None,
    metadata: dict[str, object] | None = None,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content=content,
        source=document_id,
        page=page,
        start=0,
        end=len(content),
        metadata=metadata or {},
    )


def _app_with_stack(knowledge_db: Path) -> FastAPI:
    """构造 app：service 用 InMemory 索引（检索/分块）+ catalog 读真实词法库。"""
    # 词法库先写入与 service 相同的数据（overview/list 走 catalog）。
    index = SqliteKnowledgeIndex(knowledge_db)
    index.upsert(
        [
            _chunk(
                "ml-1",
                "支持向量机是一种监督学习模型。",
                document_id="ml",
                page=1,
                metadata={
                    "subject": "机器学习",
                    "difficulty": "intermediate",
                    "title": "机器学习",
                },
            ),
            _chunk("ml-2", "间隔最大化是支持向量机的核心。", document_id="ml", page=2),
        ]
    )
    index.mark_document_complete("ml", chunk_count=2, page_count=2)
    index.close()

    # service 索引与词法库写入同一批分块、同一 chunk_id 形态：浏览/
    # chunk 端点按 ml-1/ml-2 可回溯。不能用 add_documents——它会重新
    # 生成 document_id:page:start:end 形态的 chunk_id（如 ml:1:0:15），
    # 与词法库里的 ml-1/ml-2 不一致，端点就会按词法库 id 查不到。
    index = InMemoryKnowledgeIndex()
    index.upsert(
        [
            _chunk(
                "ml-1",
                "支持向量机是一种监督学习模型。",
                document_id="ml",
                page=1,
                metadata={"subject": "机器学习", "difficulty": "intermediate"},
            ),
            _chunk(
                "ml-2",
                "间隔最大化是支持向量机的核心。",
                document_id="ml",
                page=2,
                metadata={"subject": "机器学习", "difficulty": "intermediate"},
            ),
        ]
    )
    service = KnowledgeService(index)

    app = create_app()
    app.state.knowledge_service = service
    app.state.knowledge_catalog = SqliteKnowledgeCatalog(knowledge_db)
    return app


async def _get(app: FastAPI, path: str) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


# ── /knowledge/overview ───────────────────────────────────────────


def test_overview_returns_stats_and_documents(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    response = asyncio.run(_get(app, "/knowledge/overview"))

    assert response.status_code == 200
    body = response.json()
    stats = body["stats"]
    assert stats["total_documents"] == 1
    assert stats["total_chunks"] == 2
    assert stats["total_pages"] == 2
    assert stats["frontmatter_chunks"] == 0
    documents = body["documents"]
    assert len(documents) == 1
    doc = documents[0]
    assert doc["document_id"] == "ml"
    assert doc["source"] == "ml"
    assert doc["title"] == "机器学习"
    assert doc["subjects"] == ["机器学习"]
    assert doc["difficulty"] == "intermediate"
    assert doc["chunk_count"] == 2
    assert doc["page_count"] == 2
    assert doc["ingested_at"] is not None


def test_overview_returns_503_when_catalog_missing() -> None:
    app = create_app()
    app.state.knowledge_service = KnowledgeService(InMemoryKnowledgeIndex())

    response = asyncio.run(_get(app, "/knowledge/overview"))

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "knowledge_unavailable"


# ── /knowledge/documents（catalog 合并） ───────────────────────────


def test_list_documents_includes_script_ingested_docs(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    response = asyncio.run(_get(app, "/knowledge/documents"))

    assert response.status_code == 200
    documents = response.json()["documents"]
    assert len(documents) == 1
    assert documents[0]["document_id"] == "ml"
    assert documents[0]["source"] == "ml"
    assert documents[0]["chunk_count"] == 2
    assert documents[0]["page_count"] == 2


# ── /knowledge/chunks/{chunk_id} ──────────────────────────────────


def test_get_chunk_returns_content_and_citation(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    response = asyncio.run(_get(app, "/knowledge/chunks/ml-1"))

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "支持向量机是一种监督学习模型。"
    citation = body["citation"]
    assert citation["document_id"] == "ml"
    assert citation["source"] == "ml"
    assert citation["page"] == 1
    assert citation["chunk_id"] == "ml-1"


def test_get_chunk_truncates_oversized_content(tmp_path: Path) -> None:
    app = create_app()
    # chunk_size=20000 让 _LONG_CHUNK（~12KB）成为单个超长 chunk：
    # 默认 1000 字符窗口会切成多个 ~1000 字符分块，截断逻辑无从触发。
    service = KnowledgeService(InMemoryKnowledgeIndex(), chunk_size=20000)
    service.add_documents(
        [KnowledgeDocument(document_id="long", content=_LONG_CHUNK, source="long")]
    )
    app.state.knowledge_service = service
    # chunk_id 由 service.add_documents 生成（document_id:page:start:end）。
    chunk_id = service.search("支持向量机", top_k=1)[0].citation.chunk_id

    response = asyncio.run(_get(app, f"/knowledge/chunks/{chunk_id}"))

    assert response.status_code == 200
    body = response.json()
    assert len(body["content"]) == CHUNK_CONTENT_MAX_LENGTH
    assert body["citation"]["chunk_id"] == chunk_id


def test_get_chunk_returns_404_for_missing_or_invalid(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    missing = asyncio.run(_get(app, "/knowledge/chunks/no-such-chunk"))
    assert missing.status_code == 404
    assert missing.json()["detail"]["error_code"] == "invalid_request"

    blank = asyncio.run(_get(app, "/knowledge/chunks/%20%20"))
    assert blank.status_code == 404

    oversized = asyncio.run(_get(app, "/knowledge/chunks/" + "x" * 600))
    assert oversized.status_code == 404


# ── /knowledge/documents/{id}/chunks ──────────────────────────────


def test_list_document_chunks_returns_paged_summaries(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    response = asyncio.run(_get(app, "/knowledge/documents/ml/chunks"))

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == "ml"
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # 按 (start, chunk_id) 排序稳定：两 chunk 都是 start=0，按 chunk_id 升序。
    assert [item["chunk_id"] for item in body["items"]] == ["ml-1", "ml-2"]
    first = body["items"][0]
    assert first["summary"] == "支持向量机是一种监督学习模型。"
    assert first["page"] == 1
    assert first["start"] == 0


def test_list_document_chunks_pagination(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    # offset 1 + limit 1 → 只剩第二页一项。
    response = asyncio.run(_get(app, "/knowledge/documents/ml/chunks?offset=1&limit=1"))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["chunk_id"] for item in body["items"]] == ["ml-2"]

    # limit 越界 → 422（FastAPI Query 拦截）。
    bad = asyncio.run(_get(app, "/knowledge/documents/ml/chunks?limit=999"))
    assert bad.status_code == 422
    bad_negative = asyncio.run(_get(app, "/knowledge/documents/ml/chunks?offset=-1"))
    assert bad_negative.status_code == 422


def test_list_document_chunks_empty_for_missing_document(tmp_path: Path) -> None:
    app = _app_with_stack(tmp_path / "knowledge.db")

    response = asyncio.run(_get(app, "/knowledge/documents/absent/chunks"))

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == "absent"
    assert body["total"] == 0
    assert body["items"] == []


def test_list_document_chunks_via_hybrid_production_path(tmp_path: Path) -> None:
    """生产装配路径（Hybrid 包 Sqlite 词法）：浏览端点返回分块，不走空兜底。"""
    knowledge_db = tmp_path / "knowledge.db"
    lexical = SqliteKnowledgeIndex(knowledge_db)
    lexical.upsert(
        [
            _chunk(
                "ml-1",
                "支持向量机是一种监督学习模型。",
                document_id="ml",
                page=1,
            )
        ]
    )
    lexical.close()
    hybrid = HybridKnowledgeIndex(SqliteKnowledgeIndex(knowledge_db), None)

    app = create_app()
    app.state.knowledge_service = KnowledgeService(hybrid)
    app.state.knowledge_catalog = SqliteKnowledgeCatalog(knowledge_db)

    response = asyncio.run(_get(app, "/knowledge/documents/ml/chunks"))

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == "ml"
    assert body["total"] == 1
    assert body["items"][0]["chunk_id"] == "ml-1"


class _SearchOnlyIndex:
    """只实现 search 的索引替身：chunks_of_document 缺失时列表端点兜底为空。"""

    def search(
        self,
        query: str,
        top_k: int,
        *,
        metadata_filter: dict[str, str] | None = None,
    ):
        return []


def test_list_document_chunks_falls_back_when_index_lacks_browse(
    tmp_path: Path,
) -> None:
    app = create_app()
    app.state.knowledge_service = KnowledgeService(_SearchOnlyIndex())
    app.state.knowledge_catalog = SqliteKnowledgeCatalog(tmp_path / "knowledge.db")

    response = asyncio.run(_get(app, "/knowledge/documents/ml/chunks"))

    assert response.status_code == 200
    assert response.json()["items"] == []


# ── OpenAPI 契约 ──────────────────────────────────────────────────


def test_openapi_exposes_new_knowledge_contracts() -> None:
    app = create_app()
    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]
    paths = openapi["paths"]

    # 新契约模型入 schema。
    for model in (
        "KnowledgeDocumentInfoDto",
        "KnowledgeBaseStatsDto",
        "KnowledgeOverviewResponse",
        "ChunkDetailResponse",
        "ChunkListEntry",
        "ChunkListResponse",
    ):
        assert model in schemas, model

    # 新路径可见。
    assert "/knowledge/overview" in paths
    assert "/knowledge/chunks/{chunk_id}" in paths
    assert "/knowledge/documents/{document_id}/chunks" in paths

    # ChunkDetailResponse 结构：content + citation。
    detail_schema = schemas["ChunkDetailResponse"]
    assert set(detail_schema["properties"]) == {"content", "citation"}

    # ChunkListResponse 结构：document_id / total / items。
    list_schema = schemas["ChunkListResponse"]
    assert set(list_schema["properties"]) == {"document_id", "total", "items"}
