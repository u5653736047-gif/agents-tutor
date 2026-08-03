"""API 知识检索链路装配测试（工作单 T2）。

覆盖范围（对应质量门禁的 API 测试要求）：
- 装配：lifespan 注入 search_knowledge 工具，权限声明正确
  （learning_assistant / teaching_assistant 授权，supervisor /
  evaluator 不授权）；
- 降级：向量库不存在 / 维度不匹配 / 损坏时 lifespan 正常启动并走
  词法单路（不阻断）；
- 配置：API_KNOWLEDGE_DB_PATH / API_VECTOR_DB_PATH 环境变量生效，
  工具检索到配置路径指向的库内容；API_KNOWLEDGE_EMBEDDING=hash
  强制零依赖哈希、auto 在 fastembed 不可用时自动回退。
全部用 tmp_path 构造临时词法/向量库，不依赖真实 data/ 目录；
向量库一律用哈希 provider 构造（测试不碰 fastembed 模型）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pytest import MonkeyPatch

import api.app as api_app
from api.app import create_app, create_knowledge_search_stack
from core.knowledge.embedding import HashEmbeddingProvider
from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.models import KnowledgeChunk
from core.knowledge.vector_index import SqliteVectorKnowledgeIndex
from core.state import AgentRole

_ALGEBRA_CHUNK = KnowledgeChunk(
    chunk_id="algebra-1",
    document_id="algebra",
    content="一元二次方程可以使用求根公式求解。",
    source="algebra",
    page=1,
    start=0,
    end=20,
)
_ML_CHUNK = KnowledgeChunk(
    chunk_id="ml-1",
    document_id="ml",
    content="支持向量机是一种监督学习模型。",
    source="ml",
    page=1,
    start=0,
    end=18,
)
_CHUNKS = [_ALGEBRA_CHUNK, _ML_CHUNK]


def _make_lexical_db(path: Path) -> None:
    """构造带两个分块的词法库（与 ingest 产物同构：SqliteKnowledgeIndex）。"""
    index = SqliteKnowledgeIndex(path)
    index.upsert(_CHUNKS)
    index.close()


def _make_vector_db(path: Path, dimension: int = 256) -> None:
    """构造带相同分块的哈希向量库（测试统一用哈希，避免依赖 fastembed）。"""
    index = SqliteVectorKnowledgeIndex(
        path, HashEmbeddingProvider(dimension=dimension)
    )
    index.upsert(_CHUNKS)
    index.close()


def test_knowledge_stack_enables_hybrid_when_vector_db_matches(
    tmp_path: Path,
) -> None:
    """词法 + 维度匹配的向量库 → 双路启用，工具返回融合命中。"""
    knowledge_db = tmp_path / "knowledge.db"
    vector_db = tmp_path / "vector_knowledge.db"
    _make_lexical_db(knowledge_db)
    _make_vector_db(vector_db)

    stack = create_knowledge_search_stack(knowledge_db, vector_db, embedding="hash")

    assert stack.vector_enabled is True
    result = stack.tool.invoke({"query": "一元二次方程", "top_k": 5})
    assert result["found"] is True
    assert result["hits"][0]["content"] == _ALGEBRA_CHUNK.content
    stack.close()


def test_knowledge_stack_degrades_to_lexical_when_vector_db_missing(
    tmp_path: Path,
) -> None:
    """向量库不存在 → 降级纯词法（不阻断），工具仍返回词法命中。"""
    knowledge_db = tmp_path / "knowledge.db"
    _make_lexical_db(knowledge_db)

    stack = create_knowledge_search_stack(
        knowledge_db, tmp_path / "missing.db", embedding="hash"
    )

    assert stack.vector_enabled is False
    result = stack.tool.invoke({"query": "支持向量机", "top_k": 5})
    assert result["found"] is True
    assert result["hits"][0]["content"] == _ML_CHUNK.content
    stack.close()


def test_knowledge_stack_degrades_to_lexical_on_dimension_mismatch(
    tmp_path: Path,
) -> None:
    """128 维哈希库 vs 默认 256 维哈希 → 维度不匹配自动降级（不阻断）。"""
    knowledge_db = tmp_path / "knowledge.db"
    vector_db = tmp_path / "vector_knowledge.db"
    _make_lexical_db(knowledge_db)
    _make_vector_db(vector_db, dimension=128)

    stack = create_knowledge_search_stack(knowledge_db, vector_db, embedding="hash")

    assert stack.vector_enabled is False
    result = stack.tool.invoke({"query": "一元二次方程", "top_k": 5})
    assert result["found"] is True
    assert result["hits"][0]["content"] == _ALGEBRA_CHUNK.content
    stack.close()


def test_knowledge_stack_degrades_to_lexical_on_corrupt_vector_db(
    tmp_path: Path,
) -> None:
    """向量库文件损坏（非 SQLite 内容）→ 打开失败降级词法（不阻断）。"""
    knowledge_db = tmp_path / "knowledge.db"
    vector_db = tmp_path / "vector_knowledge.db"
    _make_lexical_db(knowledge_db)
    vector_db.write_bytes(b"this is not a sqlite database")

    stack = create_knowledge_search_stack(knowledge_db, vector_db, embedding="hash")

    assert stack.vector_enabled is False
    result = stack.tool.invoke({"query": "一元二次方程", "top_k": 5})
    assert result["found"] is True
    assert result["hits"][0]["content"] == _ALGEBRA_CHUNK.content
    stack.close()


def test_knowledge_stack_rejects_unknown_embedding_mode(tmp_path: Path) -> None:
    """非法 embedding 模式在装配入口报错（配置错误暴露，不静默）。"""
    knowledge_db = tmp_path / "knowledge.db"
    _make_lexical_db(knowledge_db)

    with pytest.raises(ValueError):
        create_knowledge_search_stack(
            knowledge_db, tmp_path / "vector_knowledge.db", embedding="bogus"
        )


def test_knowledge_stack_auto_mode_prefers_fastembed_for_512_dim_vector_db(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """auto 正向路径：512 维向量库 + fastembed 可用 → FastEmbed 优先打开。

    与回退测试（test_lifespan_auto_mode_falls_back_when_fastembed_unavailable）
    相对：回退测试模拟 fastembed 不可用（构造抛错）验证降级；本测试
    模拟 fastembed 可用（stub 提供 512 维确定性向量，不联网）验证正向
    ——auto 模式用 FastEmbedProvider 匹配打开 T1 风格的 512 维库。
    若 auto 没走 FastEmbed（比如被误改成只试哈希），哈希 256 维打不开
    512 维库，vector_enabled 断言会失败——因此本测试锁定了正向路径。
    """
    knowledge_db = tmp_path / "knowledge.db"
    vector_db = tmp_path / "vector_knowledge.db"
    _make_lexical_db(knowledge_db)
    _make_vector_db(vector_db, dimension=512)  # T1 入库产物同构：512 维向量库

    class _FakeFastEmbed:
        """fastembed 的确定性替身：dimension=512、零向量（不联网）。

        零向量会被向量索引的 _normalize 安全跳过（见 vector_index.py
        注释：点积恒 0，打分时当作不相关），因此检索仍由词法路主导，
        工具调用不会因向量路而失败。
        """

        dimension = 512

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * self.dimension for _ in texts]

    monkeypatch.setattr(api_app, "FastEmbedProvider", _FakeFastEmbed)

    # 不传 embedding 参数 → 默认 auto：先试 FastEmbed（stub 成功打开
    # 512 维库）→ 命中即启用，不再轮到哈希候选。
    stack = create_knowledge_search_stack(knowledge_db, vector_db)

    assert stack.vector_enabled is True
    result = stack.tool.invoke({"query": "一元二次方程", "top_k": 5})
    assert result["found"] is True
    assert result["hits"][0]["content"] == _ALGEBRA_CHUNK.content
    stack.close()


def _lifespan_env(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    knowledge_db: Path,
    vector_db: Path,
) -> None:
    """设置 lifespan 所需环境变量：模型替身 + 全部资源路径指向 tmp。"""
    monkeypatch.setenv("DEEPSEEK_MODEL", "test-model")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("API_SESSION_STORE_PATH", str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv("API_CHECKPOINT_PATH", str(tmp_path / "checkpoints.sqlite3"))
    monkeypatch.setenv("API_KNOWLEDGE_DB_PATH", str(knowledge_db))
    monkeypatch.setenv("API_VECTOR_DB_PATH", str(vector_db))


def test_lifespan_wires_search_knowledge_tool_and_permissions(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """图持有 search_knowledge 工具，权限声明符合 prompts 角色约定。"""
    knowledge_db = tmp_path / "knowledge.db"
    _make_lexical_db(knowledge_db)
    _lifespan_env(monkeypatch, tmp_path, knowledge_db, tmp_path / "missing-vector.db")
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            graph = getattr(app.state, "graph", None)
            assert graph is not None
            assert graph.registry.get("search_knowledge") is not None
            # 授权：答疑（learning_assistant）与备课（teaching_assistant）
            # 两个产出知识内容的 Worker（理由见 app.py 模块底部注释）。
            assert graph.registry.is_authorized(
                "search_knowledge", AgentRole.LEARNING_ASSISTANT
            )
            assert graph.registry.is_authorized(
                "search_knowledge", AgentRole.TEACHING_ASSISTANT
            )
            # 最小权限：协调者与评价者不授（supervisor 是调度者、
            # evaluator 基于历史工具观察结果评价，无需自己检索）。
            assert not graph.registry.is_authorized(
                "search_knowledge", AgentRole.SUPERVISOR
            )
            assert not graph.registry.is_authorized(
                "search_knowledge", AgentRole.EVALUATOR
            )

    asyncio.run(verify_runtime())


def test_lifespan_uses_configured_knowledge_db_paths(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """API_KNOWLEDGE_DB_PATH 生效：工具检索到配置路径库中的内容。"""
    knowledge_db = tmp_path / "custom_knowledge.db"
    _make_lexical_db(knowledge_db)
    _lifespan_env(monkeypatch, tmp_path, knowledge_db, tmp_path / "missing-vector.db")
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            graph = getattr(app.state, "graph", None)
            assert graph is not None
            tool = graph.registry.get("search_knowledge")
            assert tool is not None
            result = tool.invoke({"query": "支持向量机", "top_k": 3})
            assert result["found"] is True
            assert result["hits"][0]["content"] == _ML_CHUNK.content

    asyncio.run(verify_runtime())


def test_lifespan_auto_mode_falls_back_when_fastembed_unavailable(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """fastembed 不可用（模拟未安装）→ auto 回退哈希，启动不阻断。

    同时覆盖「装配必须容忍 fastembed 不存在」的硬要求：pyproject 未
    锁定 fastembed（既定决策），生产环境不保证安装。
    """
    knowledge_db = tmp_path / "knowledge.db"
    vector_db = tmp_path / "vector_knowledge.db"
    _make_lexical_db(knowledge_db)
    _make_vector_db(vector_db)  # 256 维哈希库：回退后的哈希 provider 可打开

    class _FastEmbedUnavailable:
        """模拟未安装 fastembed：构造即抛 RuntimeError（见 embedding.py）。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("fastembed 未安装")

    monkeypatch.setattr(api_app, "FastEmbedProvider", _FastEmbedUnavailable)
    # 不设 API_KNOWLEDGE_EMBEDDING → lifespan 默认 auto 模式。
    _lifespan_env(monkeypatch, tmp_path, knowledge_db, vector_db)
    app = create_app()

    async def verify_runtime() -> None:
        async with app.router.lifespan_context(app):
            graph = getattr(app.state, "graph", None)
            assert graph is not None
            tool = graph.registry.get("search_knowledge")
            assert tool is not None
            result = tool.invoke({"query": "一元二次方程", "top_k": 3})
            assert result["found"] is True

    asyncio.run(verify_runtime())


def test_knowledge_default_paths_resolve_to_repo_root() -> None:
    """默认知识库路径按仓库根 data/ 解析（README 约定，防回归）。

    本 bug 的回归测试：曾用相对路径 "data/knowledge.db"（相对启动
    工作目录），在 backend/ 下启动/测试时落到 backend/data/——父
    目录不存在且 SQLite 不会自动创建，SqliteKnowledgeIndex 直接抛
    OperationalError。现与 scripts/ingest_books.py 的 REPO_ROOT 同一
    parents 定位惯例（app.py 位于 backend/src/api/，parents[3] =
    仓库根）解析到根目录 data/。
    """
    knowledge_db = Path(api_app.DEFAULT_KNOWLEDGE_DB_PATH)
    vector_db = Path(api_app.DEFAULT_VECTOR_DB_PATH)
    assert knowledge_db.is_absolute()
    assert vector_db.is_absolute()
    # 测试文件位于 backend/tests/，parents[2] = 仓库根，与 app.py 的
    # parents[3] 指向同一目录——默认路径必须落在仓库根 data/ 下。
    assert knowledge_db.parent.parent == Path(__file__).resolve().parents[2]
    assert vector_db.parent.parent == Path(__file__).resolve().parents[2]
    assert knowledge_db.parent.name == "data"
    assert knowledge_db.name == "knowledge.db"
    assert vector_db.name == "vector_knowledge.db"
