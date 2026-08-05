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

检索测试用 InMemoryKnowledgeIndex(与 test_knowledge_* 的 core 层
测试同一构造方式),不依赖真实 data/ 目录与 embedding 模型。
"""

from __future__ import annotations

import asyncio
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
