"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.tools import BaseTool
from starlette.responses import JSONResponse, Response

from api.approvals import router as approval_router
from api.chat import router as chat_router
from api.openapi import install_openapi_contract
from api.schemas import ApiErrorCode, ErrorDetail, ErrorResponse
from api.sessions import router as session_router
from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.embedding import (
    EmbeddingProvider,
    FastEmbedProvider,
    HashEmbeddingProvider,
)
from core.knowledge.hybrid import (
    HybridKnowledgeIndex,
    open_vector_index_if_available,
)
from core.knowledge.index import SqliteKnowledgeIndex
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.knowledge.vector_index import SqliteVectorKnowledgeIndex
from core.models import DeepSeekSettings, create_deepseek_model
from core.nodes.react_agent import ChatModel
from core.persistence import open_sqlite_checkpointer
from core.sessions import SessionStore
from core.state import AgentRole

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_API_KEY = "not-configured"
# ── 仓库根目录：所有 data/ 默认路径按仓库根解析 ────────────────────
# 与 scripts/ingest_books.py 的 REPO_ROOT（parents[2]）、
# core/models/deepseek.py 的 DEFAULT_ENV_FILE（parents[4]）同一
# 「用 __file__ 定位仓库根」惯例（app.py 位于 backend/src/api/，
# parents[3] 即仓库根）。不能写成相对启动工作目录的 "data/..."：
# uvicorn 与 pytest 都在 backend/ 下启动，相对路径会落到
# backend/data/（目录不存在，而 SQLite 不会自动创建父目录，
# SqliteKnowledgeIndex 直接抛 OperationalError）；README 约定
# 默认就是根目录 data/（start-stage3.ps1 注入的绝对路径 env 或此处
# 内置默认，均解析到根目录 data/；默认值服务手动启动与测试场景）。
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SESSION_STORE_PATH = str(_REPO_ROOT / "data" / "api_sessions.sqlite3")
DEFAULT_CHECKPOINT_PATH = str(_REPO_ROOT / "data" / "api_checkpoints.sqlite3")
# ── 知识库路径（工作单 T2）──────────────────────────────────────────
# 与 API_SESSION_STORE_PATH / API_CHECKPOINT_PATH 同一命名风格：
# API_ 前缀 + 资源名 + _PATH 后缀，默认值解析到根目录 data/
# （与 ingest 脚本的仓库根解析一致）。知识库由 S3-T1 的 ingest
# 脚本生成：
# - API_KNOWLEDGE_DB_PATH：词法库（永不降级的底线检索）；
# - API_VECTOR_DB_PATH：向量库（可选增强，不可用自动降级词法）；
# - API_KNOWLEDGE_EMBEDDING：向量 embedding 提供方模式，auto（默认）
#   优先真实语义模型、不可用时回退哈希；hash 强制零依赖哈希。
#   部署未启用 embedding extra（未安装 fastembed）时 auto 会回退哈希，
#   实际启用了哪条路以启动日志与 /healthz 的 retrieval 诊断为准。
DEFAULT_KNOWLEDGE_DB_PATH = str(_REPO_ROOT / "data" / "knowledge.db")
DEFAULT_VECTOR_DB_PATH = str(_REPO_ROOT / "data" / "vector_knowledge.db")
DEFAULT_EMBEDDING_MODE = "auto"
# ── search_knowledge 工具的角色授权声明（理由见模块注释）──────────
_KNOWLEDGE_TOOL_PERMISSIONS: dict[str, Collection[AgentRole]] = {
    "search_knowledge": frozenset(
        {AgentRole.LEARNING_ASSISTANT, AgentRole.TEACHING_ASSISTANT}
    ),
}
REQUEST_LOGGER = logging.getLogger("api.request")
_LOGGER = logging.getLogger("api.app")
RequestHandler = Callable[[Request], Awaitable[Response]]


@dataclass(frozen=True, slots=True)
class KnowledgeSearchStack:
    """知识检索链路的装配结果：检索工具 + 释放回调 + 向量路是否启用。

    - tool：已包装 KnowledgeService 的 search_knowledge 工具，交给图注入；
    - close：关闭底层索引（词法/向量 SQLite 连接）的回调，lifespan
      退出时调用，避免连接泄漏；
    - vector_enabled：向量路是否真的打开（False = 降级为纯词法），
      供日志与测试观测，不在图里暴露；
    - vector_provider / vector_dimension（H-T1 诊断字段）：向量路成功
      打开时使用的 provider 类名与其向量维度（如 "HashEmbeddingProvider"
      / 256、"FastEmbedProvider" / 512），向量路未打开时为 None。
      供启动日志与 /healthz 诊断「语义检索是否在线」，不参与检索。
    """

    tool: BaseTool
    close: Callable[[], None]
    vector_enabled: bool
    vector_provider: str | None = None
    vector_dimension: int | None = None


def _embedding_provider_candidates(mode: str) -> list[EmbeddingProvider]:
    """按模式返回候选 embedding provider 列表（按序尝试，先成功先启用）。

    为什么需要候选列表而不是只选一个 provider（面向初学者）：
    - 向量库是用「入库时的 provider 维度」写入的：T1 用 fastembed
      （512 维）重建过 data/vector_knowledge.db；哈希替身默认 256 维。
      只给一个 provider，库的维度与它不匹配时 open_vector_index_if_available
      会返回 None 整体降级词法——浪费了另一条向量路；
    - 逐个尝试：512 维库由 FastEmbedProvider 打开，256 维哈希库由
      HashEmbeddingProvider 打开，两代入库产物都能用上向量路；
    - 全部失败（库损坏等）由 open_vector_index_if_available 各自吞掉
      并返回 None，最终降级纯词法，不阻断启动。
    模式说明：
    - "auto"（默认）：优先 FastEmbedProvider（真实语义，匹配 T1 的
      512 维库）。fastembed 未安装 / 模型不可用（构造抛 ImportError /
      RuntimeError / OSError）时回退哈希——fastembed 是可选依赖组
      embedding（uv lock 已锁定），默认 uv sync --extra dev 不安装，
      装配必须容忍它不存在；
    - "hash"：强制 HashEmbeddingProvider，零依赖、零模型下载、行为
      完全确定——测试与完全离线部署用它，避免启动时碰模型。
    """
    if mode == "hash":
        return [HashEmbeddingProvider()]
    if mode != "auto":
        # 配置错误要暴露而不是静默当成 auto（与 graph_builder 的
        # 配置校验同一哲学：拼写错误应让运维立刻发现）。
        raise ValueError("API_KNOWLEDGE_EMBEDDING 只支持 auto 或 hash")
    candidates: list[EmbeddingProvider] = []
    try:
        # 真实语义模型优先：只有它能匹配 T1 的 512 维 fastembed 库
        #（哈希 256 维打开会因维度不匹配被拒，等于浪费向量路）。
        candidates.append(FastEmbedProvider())
    except (ImportError, RuntimeError, OSError):
        # 未安装 fastembed / 模型加载失败 → 降级哈希（不阻断启动，
        # 见 embedding.py FastEmbedProvider 的惰性导入说明）。
        pass
    # 哈希始终兜底：256 维哈希库能直接打开；512 维 fastembed 库
    # 打不开（维度不匹配 ValueError 被 hybrid 层吞掉）则返回 None，
    # 由调用方决定是否试下一个候选——候选本身不抛错。
    candidates.append(HashEmbeddingProvider())
    return candidates


def create_knowledge_search_stack(
    knowledge_db: Path,
    vector_db: Path,
    *,
    embedding: str = DEFAULT_EMBEDDING_MODE,
) -> KnowledgeSearchStack:
    """装配知识检索链路：词法库 → 向量库（可选）→ 混合索引 → 服务 → 工具。

    装配链路（面向初学者，自底向上）：
        SqliteKnowledgeIndex(词法库)          ← 底线检索，永不降级
              ↓
        open_vector_index_if_available(向量库, provider)  ← 可用才开，
        （文件不存在 / 维度不匹配 / 损坏 → None，不抛错）    不可用自动降级
              ↓
        HybridKnowledgeIndex(词法, 向量或 None)  ← 两路 RRF 融合；
                                                  向量为 None 时纯词法
              ↓
        KnowledgeService(混合索引)              ← 服务层（校验、可选改写）
              ↓
        create_search_knowledge_tool(服务)      ← Agent 可调用的工具

    降级语义：向量库是可选增强——文件不存在、维度与 provider 不匹配
    （换过 embedding 未重建库）、SQLite 损坏，任一情况都返回 None
    降级为纯词法，启动不阻断。词法库相反：它是底线底座，文件打不开
    （损坏 / 权限）属于环境错误，向上抛出让运维发现，而不是静默
    启动一个没有知识的服务。
    资源释放：返回的 stack.close() 会关闭词法与向量两个 SQLite 连接
    （HybridKnowledgeIndex.close 转发给两路，见 hybrid.py）。
    """
    # 模式校验在入口统一做：即使向量库不存在（不构造 provider）也要
    # 暴露拼写错误，而不是静默当成 auto（配置错误应让运维立刻发现）。
    if embedding not in ("auto", "hash"):
        raise ValueError("API_KNOWLEDGE_EMBEDDING 只支持 auto 或 hash")
    # 词法库构造会建表（与 SessionStore/checkpointer 一致的自动建库
    # 行为）：首次启动没有知识库时得到空库，工具可用但检索为空，
    # 不阻断服务启动。
    lexical = SqliteKnowledgeIndex(knowledge_db)
    vector: SqliteVectorKnowledgeIndex | None = None
    # H-T1 诊断字段：记录「向量路被哪个 provider 打开、维度多少」，
    # 供启动日志与 /healthz 观测「语义检索是否在线」；向量路未打开
    # （文件不存在 / 维度不匹配 / 损坏）时保持 None。
    vector_provider: str | None = None
    vector_dimension: int | None = None
    if Path(vector_db).exists():
        # 文件存在才尝试打开（hybrid 层自己也会检查；这里提前检查
        # 是为了在「没有向量库」的环境里不构造 provider——避免
        # auto 模式下白加载/下载 embedding 模型）。
        for provider in _embedding_provider_candidates(embedding):
            vector = open_vector_index_if_available(vector_db, provider=provider)
            if vector is not None:
                # 记录真正打开向量库的 provider（类名 + 维度），
                # 供日志与 /healthz 诊断，不参与检索本身。
                vector_provider = type(provider).__name__
                vector_dimension = provider.dimension
                break
    hybrid = HybridKnowledgeIndex(lexical, vector)
    service = KnowledgeService(hybrid)
    return KnowledgeSearchStack(
        tool=create_search_knowledge_tool(service),
        close=hybrid.close,
        vector_enabled=hybrid.vector_enabled,
        vector_provider=vector_provider,
        vector_dimension=vector_dimension,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create shared core resources for the lifetime of the API application."""
    model_settings = DeepSeekSettings(
        model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.getenv("DEEPSEEK_API_KEY", DEFAULT_API_KEY),
    )
    session_store_path = Path(
        os.getenv("API_SESSION_STORE_PATH", DEFAULT_SESSION_STORE_PATH)
    )
    checkpoint_path = Path(os.getenv("API_CHECKPOINT_PATH", DEFAULT_CHECKPOINT_PATH))
    knowledge_db = Path(os.getenv("API_KNOWLEDGE_DB_PATH", DEFAULT_KNOWLEDGE_DB_PATH))
    vector_db = Path(os.getenv("API_VECTOR_DB_PATH", DEFAULT_VECTOR_DB_PATH))

    with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        session_store = SessionStore(session_store_path)
        # 装配知识检索链路（工作单 T2）：词法 → 向量（可选）→ 混合 →
        # 服务 → 工具；向量不可用自动降级词法，不阻断启动（详见
        # create_knowledge_search_stack 注释）。词法库打不开会在此
        # 抛出环境错误，服务启动失败——这是有意的暴露而非静默降级。
        knowledge_stack = create_knowledge_search_stack(
            knowledge_db,
            vector_db,
            embedding=os.getenv("API_KNOWLEDGE_EMBEDDING", DEFAULT_EMBEDDING_MODE),
        )
        # H-T1 统一结构化启动日志：hybrid / lexical_only 都打，让运维
        # 一眼看出语义检索是否在线。只打模式 / provider / 维度，不打印
        # 任何文件路径（旧日志打印 vector_db 绝对路径，属部署细节）。
        mode = "hybrid" if knowledge_stack.vector_enabled else "lexical_only"
        _LOGGER.info(
            "知识检索模式=%s embedding_provider=%s vector_dimension=%s",
            mode,
            knowledge_stack.vector_provider,
            knowledge_stack.vector_dimension,
        )
        # 诊断快照挂到 app.state，供 /healthz 输出（字段只含 mode /
        # provider / 维度，绝不含路径；lifespan 未跑或已退出时该属性
        # 不存在/为 None，/healthz 用 getattr 兜底保持现状）。挂在 try
        # 内：图装配失败时不留「与实际不符」的快照。
        try:
            app.state.graph = CollaborativeAgentGraph(
                model=cast(ChatModel, create_deepseek_model(model_settings)),
                checkpointer=checkpointer,
                interrupt_before_handoff=True,
                # 业务工具与授权：search_knowledge 只授给需要产出知识
                # 内容的两个 Worker（理由见模块底部 _KNOWLEDGE_TOOL_PERMISSIONS
                # 的注释）；graph_builder 会校验权限声明完整（缺工具或
                # 权限为 None 会抛 ValueError）。
                tools=[knowledge_stack.tool],
                tool_permissions=_KNOWLEDGE_TOOL_PERMISSIONS,
            )
            app.state.session_store = session_store
            app.state.retrieval_diagnostics = {
                "mode": mode,
                "embedding_provider": knowledge_stack.vector_provider,
                "vector_dimension": knowledge_stack.vector_dimension,
            }
            yield
        finally:
            # 释放顺序：先关知识索引（图已不再执行，工具闭包不再被
            # 调用），再关会话库，最后清空状态引用。诊断快照一并清除，
            # 与「lifespan 未跑时 /healthz 不带 retrieval」语义一致。
            knowledge_stack.close()
            session_store.close()
            app.state.graph = None
            app.state.session_store = None
            app.state.retrieval_diagnostics = None


def create_app() -> FastAPI:
    """Create the API application."""
    app = FastAPI(lifespan=lifespan)
    app.state.chat_session_locks = {}
    install_openapi_contract(app)
    app.include_router(chat_router)
    app.include_router(approval_router)
    app.include_router(session_router)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        """Avoid returning validation inputs or framework-specific error details."""
        error = ErrorResponse(
            detail=ErrorDetail(
                error_code=ApiErrorCode.INVALID_REQUEST,
                message="Request is invalid.",
            )
        )
        return JSONResponse(status_code=422, content=error.model_dump(mode="json"))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_request(request: Request, call_next: RequestHandler) -> Response:
        started_at = time.perf_counter()
        response = await call_next(request)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        REQUEST_LOGGER.info(
            "request_complete method=%s path=%s status=%s duration_ms=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, object]:
        """存活探针；lifespan 装配后附带检索模式诊断（H-T1）。

        - lifespan 未跑（如单测直接 create_app()）或诊断未就绪：保持
          {"status": "ok"} 现状，不破坏既有探针语义与测试；
        - lifespan 跑过：附加 retrieval 字段（mode / embedding_provider /
          vector_dimension），运维据此判断语义检索是否在线。诊断只含
          这三个字段，绝不含任何文件路径。
        """
        diagnostics = getattr(request.app.state, "retrieval_diagnostics", None)
        if diagnostics is None:
            return {"status": "ok"}
        return {"status": "ok", "retrieval": diagnostics}

    return app


# ── search_knowledge 工具的角色授权理由（工作单 T2）────────────────
# 依据 core/nodes/prompts.py 各 Agent 的角色约定：
# - learning_assistant（助学助手，答疑与学习规划）：授权。答疑必须
#   基于教材内容分层讲解（prompt 要求按学生水平引用知识），且 S2-T4
#   的引用机制（references 元数据 + evaluator 的引用完整性校验）以
#   检索证据为前提——不授权则答疑无据可依；
# - teaching_assistant（助教，知识讲解与备课支持）：授权。备课要生成
#   教案/讲解材料，必须引用教材内容，否则就是凭空编造（与引用机制
#   的设计意图冲突）；
# - supervisor（协调者）：不授权。prompt 中它是调度者（意图识别、
#   handoff、任务计划），简单答疑直接回答、复杂答疑转 learning_assistant
#   ——不直接产出知识内容，保持最小权限；
# - evaluator（评价者）：不授权。prompt 要求「基于本轮最终回答与检索
#   证据（工具观察结果）评价」——检索证据来自其他 Agent 调用工具的
#   观察结果（消息历史中的 ToolMessage），不需要自己调检索工具；
#   且引用校验（chunk 级真实命中比对）由核心侧确定性完成，不依赖
#   evaluator 检索。
# 若未来 supervisor 需要直接引用知识作答，在此追加角色即可（graph_builder
# 只要求权限声明完整，不限制谁可用）。
__all__ = ["create_app", "create_knowledge_search_stack"]
