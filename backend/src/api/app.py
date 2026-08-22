"""FastAPI application factory."""

from __future__ import annotations

import logging
import math
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
from api.feedback import router as feedback_router
from api.files import router as files_router
from api.knowledge import router as knowledge_router
from api.learning import router as learning_router
from api.openapi import install_openapi_contract
from api.schemas import ApiErrorCode, ErrorDetail, ErrorResponse
from api.sessions import router as session_router
from api.stats import router as stats_router
from api.stream import router as stream_router
from api.tool_approvals import router as tool_approval_router
from api.workspaces import router as workspace_router
from core.filesystem import WorkspaceFileSystem
from core.graph_builder import CollaborativeAgentGraph
from core.knowledge.catalog import SqliteKnowledgeCatalog
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
from core.knowledge.llm_rewriter import LLMQueryRewriter
from core.knowledge.policy import HeuristicRetrievalPolicy, RetrievalPolicy
from core.knowledge.reranker import DEFAULT_RERANK_MODEL, FastEmbedReranker
from core.knowledge.retrieval import (
    HeuristicQueryRefiner,
    QueryRefiner,
    QueryRewriter,
    Reranker,
)
from core.knowledge.service import KnowledgeService
from core.knowledge.tools import create_search_knowledge_tool
from core.knowledge.vector_index import SqliteVectorKnowledgeIndex
from core.learning import LearningRecordStore
from core.models import DeepSeekSettings, create_deepseek_model
from core.nodes.react_agent import ChatModel
from core.ocr import OcrProvider, create_ocr_provider
from core.pdf_table import resolve_pdf_table_mode
from core.persistence import open_sqlite_checkpointer
from core.sessions import SessionStore
from core.state import AgentRole
from core.tools import (
    MAX_SHELL_TIMEOUT_SECONDS,
    OFFICECLI_TIMEOUT_MARGIN_SECONDS,
    create_office_tools,
    create_read_only_file_tools,
    create_shell_tool,
    load_officecli_settings,
    officecli_enabled,
)
from core.vision import create_vision_provider

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
# ── 学习记录库路径（六大功能计划 P0-4）────────────────────
# 与 API_KNOWLEDGE_DB_PATH 同一命名风格：学情诊断/路径规划/陪伴的
# 跨会话学生数据底座（data/learning.db，WAL，见 core/learning/store.py）。
DEFAULT_LEARNING_DB_PATH = str(_REPO_ROOT / "data" / "learning.db")
DEFAULT_OCR_MODE = "auto"
# ── RAG 增强组件开关（S5：改写/重排接线）────────────────────────────
# 与 API_KNOWLEDGE_EMBEDDING / API_OCR_MODE 同一「auto|off」约定：
# - API_KNOWLEDGE_REWRITE：auto（默认）= 已配置模型 key 时装配 LLM
#   查询改写器（多变体联合检索提升召回）；off = 强制关闭；未配置
#   key（评委/CI 环境）时 auto 自动跳过，避免每次检索白调必失败的模型。
# - API_KNOWLEDGE_RERANK：auto（默认）= fastembed 可用时装配
#   Cross-Encoder 重排器（精排初检候选）；构造失败（未安装/模型不可
#   用）自动降级为不重排，不阻断启动；off = 强制关闭。
# - API_RERANK_MODEL：重排模型名（默认 bge-reranker-base）。
DEFAULT_REWRITE_MODE = "auto"
DEFAULT_RERANK_MODE = "auto"
# ── 上下文预算默认值（六大功能计划 P0-1）───────────────────
# 背后模型为 1M 窗口，512K 是**护栏上限而非目标填充量**：批改整份
# PDF 作业正文 + 评分依据检索 + 多轮历史才可能逼近，普通对话远达
# 不到；内置估算器按中文 1 字符≈1 token 保守高估，实际送入模型的
# token 少于预算值，方向安全（不会击穿模型窗口）。权衡提示：ReAct
# 每轮 model.invoke 全量重放历史（react_agent.py），极端长会话的输入
# 成本与 prefill 延迟随历史增长，按 extra["context_token_count"]
# 埋点观测，必要时环境变量下调。附带收益：大窗口 + 裁剪护栏已覆盖
# 长对话，TASK_BREAKDOWN 1.3.2 的「长对话摘要压缩」可继续不做。
DEFAULT_MAX_CONTEXT_TOKENS = 524288
DEFAULT_MAX_CONTEXT_MESSAGES = 200
# 自适应检索相关性阈值（六大功能计划 P0-2）：量纲跟随索引分数——
# 生产混合检索为 RRF 融合分（单项第 1 名 ≈ 1/61 ≈ 0.0164，双路
# 第 1 名 ≈ 0.0328，见 hybrid.py 模块注释），0.01 约可滤掉排名≈40
# 以后的极低相关命中；纯词法降级模式分数为命中词数（≥ 1），全部
# 达标不受影响。未达标时工具 Observation 会提示「知识库可能未覆盖」
# （tools.py _THRESHOLD_MISS_HINT），Agent 应如实说明而非强行作答。
DEFAULT_RETRIEVAL_THRESHOLD = 0.01
# 未显式配置时只开放进程工作目录，绝不通过源码层级推导到磁盘根目录。
# 本地启动脚本会显式设为仓库根；容器显式设为只读挂载的 /workspace。
DEFAULT_WORKSPACE_ROOT = str(Path.cwd().resolve())
# ── search_knowledge 工具的角色授权声明（理由见模块注释）──────────
_KNOWLEDGE_TOOL_PERMISSIONS: dict[str, Collection[AgentRole]] = {
    # 六大功能 P2-11：evaluator 加入检索授权（有意逆转既定默认，理由
    # 更新见模块底部注释）——批改场景 loop 中无其他 Agent 产出检索
    # 证据，评分依据必须对齐教材检索（客观题佐证 + 主观题评分标准）。
    "search_knowledge": frozenset(
        {
            AgentRole.LEARNING_ASSISTANT,
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.EVALUATOR,
        }
    ),
}
_READ_ONLY_FILE_TOOL_PERMISSIONS: dict[str, Collection[AgentRole]] = {
    tool_name: frozenset(
        {
            AgentRole.SUPERVISOR,
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.LEARNING_ASSISTANT,
        }
    )
    for tool_name in (
        "workspace_info",
        "list_files",
        "glob_files",
        "grep_files",
        "read_file",
        "inspect_workspace",
    )
}
_SHELL_TOOL_PERMISSIONS: dict[str, Collection[AgentRole]] = {
    "shell": frozenset({AgentRole.SUPERVISOR}),
}
# ── officecli 工具的角色授权（计划 3.9 权限矩阵）──────────────────
# officecli_inspect 只读，四个角色均可用；officecli_edit 有副作用且需
# 人工审批，授给 Supervisor / 助教 / 评价，助学（面向学生的答疑角色）
# 不具备文档写权限。注意：API_OFFICECLI_ENABLED=0 时工具不注册，本
# 表也必须同步省略——graph_builder 会拒绝「权限声明了未注册工具」。
_OFFICE_TOOL_PERMISSIONS: dict[str, Collection[AgentRole]] = {
    "officecli_inspect": frozenset(
        {
            AgentRole.SUPERVISOR,
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.LEARNING_ASSISTANT,
            AgentRole.EVALUATOR,
        }
    ),
    "officecli_edit": frozenset(
        {
            AgentRole.SUPERVISOR,
            AgentRole.TEACHING_ASSISTANT,
            AgentRole.EVALUATOR,
        }
    ),
}
REQUEST_LOGGER = logging.getLogger("api.request")
_LOGGER = logging.getLogger("api.app")
RequestHandler = Callable[[Request], Awaitable[Response]]


def _env_positive_int(name: str, default: int) -> int:
    """读取正整数环境变量；缺失/非法/非正时回退默认值（警告日志）。

    配置错误的处置取舍：与 embedding/OCR 模式的 fail-fast 不同，
    上下文预算是护栏参数而非能力开关——非法值回退安全默认不阻断
    启动，但要留警告日志让运维发现拼写错误。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        _LOGGER.warning("环境变量 %s 非法（%r），回退默认值 %s", name, raw, default)
        return default
    if value <= 0:
        _LOGGER.warning("环境变量 %s 非正（%s），回退默认值 %s", name, value, default)
        return default
    return value


def _env_positive_float(name: str, default: float) -> float:
    """读取正浮点环境变量；缺失/非法/非正/非有限时回退默认（审查 S4）。

    与 _env_positive_int 同一护栏哲学：检索阈值是护栏参数而非能力
    开关——裸 float() 会让拼写错误（"0,01"）直接崩启动，而
    float("nan") 使比较恒 False（阈值静默失效）、float("inf") 使
    所有命中被判未达标（语义反转），三者都必须回退安全默认并留日志。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        _LOGGER.warning("环境变量 %s 非法（%r），回退默认值 %s", name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        _LOGGER.warning("环境变量 %s 非正或非有限（%s），回退默认值 %s", name, value, default)
        return default
    return value


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
    - rewrite_enabled / reranker_enabled（S5 诊断字段）：LLM 查询改写
      器与 Cross-Encoder 重排器是否实际装配启用，供启动日志与
      /healthz 诊断「检索增强是否在线」，不参与检索。
    """
    
    tool: BaseTool
    close: Callable[[], None]
    vector_enabled: bool
    vector_provider: str | None = None
    vector_dimension: int | None = None
    rewrite_enabled: bool = False
    reranker_enabled: bool = False
    # D6-T3:检索服务实例,随装配结果一起暴露——lifespan 把它挂到
    # app.state.knowledge_service 供 REST 路由注入(见 api/knowledge.py),
    # 与 search_knowledge 工具共用同一实例,检索行为一致。
    service: KnowledgeService | None = None
    # I1:知识库清单服务实例,随装配结果一起暴露——lifespan 把它挂到
    # app.state.knowledge_catalog 供 REST 清单/总览路由注入(见
    # api/knowledge.py 的 get_knowledge_catalog),与检索共用同一词法库。
    catalog: SqliteKnowledgeCatalog | None = None


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
    except Exception as exc:  # noqa: BLE001 — 可选能力探测，降级是设计意图
        # 未安装 fastembed / 模型加载失败 → 降级哈希（不阻断启动，
        # 见 embedding.py FastEmbedProvider 的惰性导入说明）。
        # 为什么拓宽到 Exception（S5）：模型首次下载走网络，httpx
        # ConnectError 等传输异常不继承 OSError，离线/网络抖动环境下
        # 原来的三类收窄捕获会让「可选增强不可用」击穿启动——与
        # retrieval.py 的 _safe_* 同一「外部组件任何异常都意味着不可用」
        # 哲学；不捕获 BaseException。
        _LOGGER.warning(
            "FastEmbedProvider 不可用（%s），回退哈希 embedding",
            type(exc).__name__,
        )
    # 哈希始终兜底：256 维哈希库能直接打开；512 维 fastembed 库
    # 打不开（维度不匹配 ValueError 被 hybrid 层吞掉）则返回 None，
    # 由调用方决定是否试下一个候选——候选本身不抛错。
    candidates.append(HashEmbeddingProvider())
    return candidates


def _env_mode(name: str, default: str, allowed: frozenset[str]) -> str:
    """读取枚举型环境变量（auto|off 类开关）；缺失/空白回退默认，非法值抛错。

    与 _env_positive_int/float 的「非法回退默认 + 警告」刻意不同：
    模式开关是能力配置而非护栏参数——拼写错误（如 "auot"）若静默
    回退默认，运维会误以为增强已启用/已关闭，因此与
    API_KNOWLEDGE_EMBEDDING / API_OCR_MODE 同一哲学：非法值尽早
    暴露（启动期 ValueError），空白视为未配置回退默认。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if value not in allowed:
        raise ValueError(
            f"环境变量 {name} 只支持 {sorted(allowed)}，实际为 {value!r}"
        )
    return value


def _create_query_rewriter(
    mode: str, settings: DeepSeekSettings
) -> LLMQueryRewriter | None:
    """按模式装配 LLM 查询改写器（S5）；不可用时返回 None（降级，不抛错）。

    模式语义与 embedding/OCR 同一约定（配置拼写错误要暴露）：
    - "off"：强制关闭，返回 None（检索走原始 query 单路，零回归）；
    - "auto"（默认）：已配置真实模型 key 时装配；未配置（默认值
      "not-configured"，评委/CI 环境）→ None——没有 key 时装配出来
      也只会每次检索白调一次必失败的模型，不如明确跳过；
    - 其它值：抛 ValueError（与 embedding 模式校验同一哲学）。

    改写模型用独立轻量实例（timeout/max_retries/max_tokens 收紧）：
    改写是 ReAct 中间轮的辅助调用，失败会由检索层降级为原始 query，
    重试没有收益；紧超时把改写延迟限定在可控范围，与主对话模型互
    不影响（详见 core/knowledge/llm_rewriter.py 模块注释第 5 节）。
    """
    if mode == "off":
        return None
    if mode != "auto":
        raise ValueError("API_KNOWLEDGE_REWRITE 只支持 auto 或 off")
    if settings.api_key == DEFAULT_API_KEY:
        return None
    # cast 与图装配处同一先例（见下方 CollaborativeAgentGraph 的
    # model=cast(ChatModel, ...)）：ChatOpenAI 的 invoke 形参名与协议
    # 不同（input vs messages），类型层面不可直接判配，行为层面满足。
    rewrite_model = cast(
        ChatModel,
        create_deepseek_model(settings, timeout=10, max_retries=0, max_tokens=128),
    )
    return LLMQueryRewriter(rewrite_model)


def _create_reranker(mode: str, model_name: str) -> FastEmbedReranker | None:
    """按模式装配 Cross-Encoder 重排器（S5）；不可用时返回 None（降级）。

    模式语义与 _create_query_rewriter 同一约定：
    - "off"：强制关闭，返回 None（检索保持初检顺序，零回归）；
    - "auto"（默认）：尝试构造 FastEmbedReranker；fastembed 未安装 /
      模型不可用（含模型下载的网络异常——httpx 传输异常不继承
      OSError）→ None 降级为不重排，不阻断启动（与 embedding/OCR
      的「可用才开」同一哲学）；
    - 其它值：抛 ValueError（配置错误要暴露，不静默当成 auto）。
    注意：首次构造会联网下载重排模型（一次性，之后离线）。
    """
    if mode == "off":
        return None
    if mode != "auto":
        raise ValueError("API_KNOWLEDGE_RERANK 只支持 auto 或 off")
    try:
        return FastEmbedReranker(model_name=model_name)
    except Exception as exc:  # noqa: BLE001 — 可选能力探测，降级是设计意图
        # 与 retrieval.py 的 _safe_rerank 同一哲学：重排是可选增强，构造
        # 失败（未安装 fastembed / 模型下载网络异常 / 模型名非法）都意味着
        # 「重排不可用」，不应阻断启动。为什么捕 Exception 而不是更窄的
        # 类型：模型首次下载走网络，httpx ConnectError 等传输异常不继承
        # OSError，收窄捕获在离线/抖动环境会让启动被可选增强击窊；
        # 不捕获 BaseException。
        _LOGGER.warning("重排器不可用（%s），降级为不重排", type(exc).__name__)
        return None


def create_knowledge_search_stack(
    knowledge_db: Path,
    vector_db: Path,
    *,
    embedding: str = DEFAULT_EMBEDDING_MODE,
    adaptive_policy: RetrievalPolicy | None = None,
    relevance_threshold: float | None = None,
    query_refiner: QueryRefiner | None = None,
    query_rewriter: QueryRewriter | None = None,
    reranker: Reranker | None = None,
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

    自适应检索装配（六大功能计划 P0-2）：adaptive_policy /
    relevance_threshold / query_refiner 默认全 None——工具走原
    service.search 路径，输出与接入前逐项一致（测试路径零回归）；
    生产 lifespan 显式注入（寒暄/纯计算免检索的启发式策略 + 相关性
    阈值 + 零 LLM 启发式精化器）后走 adaptive 路径。

    S5 检索增强装配：query_rewriter / reranker 默认 None——不走
    改写与重排，行为与接入前逐项一致（零回归）；生产 lifespan 按
    API_KNOWLEDGE_REWRITE / API_KNOWLEDGE_RERANK 装配
    LLMQueryRewriter 与 FastEmbedReranker 后启用（改写延迟控制与
    重排模型选型见 core/knowledge/llm_rewriter.py、reranker.py 的
    模块注释）。重排只改顺序不改 score（reranker.py 第 3 节），
    relevance_threshold 的量纲语义不受重排影响。

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
    # P0-2：精化器注入时重检上限取 1（每轮重检 = 一次完整检索，
    # 启发式精化的收益不值得多轮；未注入精化器时该参数无实际作用）。
    service = KnowledgeService(
        hybrid,
        max_refine_rounds=1 if query_refiner is not None else 2,
        rewriter=query_rewriter,
        reranker=reranker,
    )
    # I1:catalog 是独立连接(与索引相同的 RLock + check_same_thread=False
    # 线程安全约定,见 catalog.py 模块 docstring)。它不在 hybrid.close
    # 的转发范围内(hybrid 只关词法/向量两路),因此 close 回调要额外
    # 关闭 catalog——用一个组合回调保证索引与清单的连接都不泄漏。
    catalog = SqliteKnowledgeCatalog(knowledge_db)

    def _close() -> None:
        hybrid.close()
        catalog.close()

    return KnowledgeSearchStack(
        tool=create_search_knowledge_tool(
            service,
            policy=adaptive_policy,
            relevance_threshold=relevance_threshold,
            refiner=query_refiner,
        ),
        close=_close,
        vector_enabled=hybrid.vector_enabled,
        vector_provider=vector_provider,
        vector_dimension=vector_dimension,
        rewrite_enabled=query_rewriter is not None,
        reranker_enabled=reranker is not None,
        # D6-T3:service 随 stack 暴露,让 lifespan 挂到 app.state 供
        # REST 检索路由使用(与工具共用同一实例,见 dataclass 注释)。
        service=service,
        # I1:catalog 随 stack 暴露,让 lifespan 挂到 app.state 供
        # REST 清单/总览路由使用(见 dataclass 注释)。
        catalog=catalog,
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
    # P0-4：学习记录库（学情诊断/路径规划/陪伴的跨会话数据底座）。
    learning_db = Path(os.getenv("API_LEARNING_DB_PATH", DEFAULT_LEARNING_DB_PATH))
    workspace_root = Path(os.getenv("API_WORKSPACE_ROOT", DEFAULT_WORKSPACE_ROOT))
    raw_allowed_workspace_roots = os.getenv("API_WORKSPACE_ALLOWED_ROOTS")
    allowed_workspace_roots = (
        [
            Path(value.strip())
            for value in raw_allowed_workspace_roots.split(os.pathsep)
            if value.strip()
        ]
        if raw_allowed_workspace_roots
        else None
    )
    workspace_filesystem = WorkspaceFileSystem(workspace_root)
    read_only_file_tools = create_read_only_file_tools(workspace_filesystem)
    shell_tool = create_shell_tool(workspace_filesystem)
    # P0-4 装配链路：学习记录 store 随 lifespan 创建/注入/关闭
    #（仿 knowledge_stack 的生命周期管理；传入图的可选构造参数
    # learning_store 后条件注册两个学习记录工具，见 graph_builder）。
    learning_store = LearningRecordStore(learning_db)
    # P0-6：OCR provider 按模式装配（auto=探测到依赖才启用），
    # 不可用时 None——附件提取链路返回友好提示而非报错；
    # 挂 app.state 供批改附件消费（P2-7），/healthz 诊断可观测。
    ocr_provider: OcrProvider | None = create_ocr_provider(
        os.getenv("API_OCR_MODE", DEFAULT_OCR_MODE)
    )
    # S5-B3：视觉理解 provider 按模式装配（auto=配置了端点才启用），
    # 附件图片三级降级链的第一级；未配置时 None，行为与现状一致。
    vision_provider = create_vision_provider(
        os.getenv("API_VISION_MODE", "auto")
    )
    # S5-B1：PDF 表格模式启动期预检（与 API_VISION_MODE 同一口径——
    # 配置拼写错误在部署时暴露，而不是首个带附件请求才失败）。
    resolve_pdf_table_mode()
    # officecli 集成（计划 3.5）：默认禁用（ENABLED=0 时完全不注册工具、
    # 不做任何二进制探测，保证无 officecli 的 CI/评委环境不受影响）；
    # 显式开启时解析二进制并启动自检，失败 fail-fast。
    office_tools: tuple[BaseTool, ...] = ()
    office_tool_permissions: dict[str, Collection[AgentRole]] = {}
    office_tool_timeouts: dict[str, float] = {}
    if officecli_enabled():
        office_settings = load_officecli_settings()
        office_tools = create_office_tools(workspace_filesystem, office_settings)
        office_tool_permissions = dict(_OFFICE_TOOL_PERMISSIONS)
        # 双层超时推导（计划 M2）：执行器时限 = 子进程超时 + 5 秒，
        # 从同一常量推导，保证子进程先超时并返回自带诊断。
        office_tool_timeouts = {
            "officecli_inspect": float(
                office_settings.timeout_read_seconds + OFFICECLI_TIMEOUT_MARGIN_SECONDS
            ),
            "officecli_edit": float(
                office_settings.timeout_write_seconds + OFFICECLI_TIMEOUT_MARGIN_SECONDS
            ),
        }

    with open_sqlite_checkpointer(checkpoint_path) as checkpointer:
        session_store = SessionStore(
            session_store_path,
            default_workspace_root=workspace_root,
            allowed_workspace_roots=allowed_workspace_roots,
        )
        # S5 检索增强装配：LLM 改写器 + Cross-Encoder 重排器（默认
        # auto，不可用时各自降级为 None，不阻断启动；模式校验与降级
        # 语义见 _create_query_rewriter / _create_reranker 注释）。
        query_rewriter = _create_query_rewriter(
            _env_mode(
                "API_KNOWLEDGE_REWRITE",
                DEFAULT_REWRITE_MODE,
                frozenset({"auto", "off"}),
            ),
            model_settings,
        )
        reranker = _create_reranker(
            _env_mode(
                "API_KNOWLEDGE_RERANK",
                DEFAULT_RERANK_MODE,
                frozenset({"auto", "off"}),
            ),
            (os.getenv("API_RERANK_MODEL") or "").strip() or DEFAULT_RERANK_MODEL,
        )
        # 装配知识检索链路（工作单 T2）：词法 → 向量（可选）→ 混合 →
        # 服务 → 工具；向量不可用自动降级词法，不阻断启动（详见
        # create_knowledge_search_stack 注释）。词法库打不开会在此
        # 抛出环境错误，服务启动失败——这是有意的暴露而非静默降级。
        knowledge_stack = create_knowledge_search_stack(
            knowledge_db,
            vector_db,
            embedding=os.getenv("API_KNOWLEDGE_EMBEDDING", DEFAULT_EMBEDDING_MODE),
            # P0-2 生产接线：寒暄/纯计算免检索的启发式策略 + 相关性
            # 阈值 + 零 LLM 启发式精化器（阈值量纲说明见
            # DEFAULT_RETRIEVAL_THRESHOLD 注释，env 可覆盖）。
            adaptive_policy=HeuristicRetrievalPolicy(),
            # 审查 S4：阈值用护栏解析（非法/nan/inf 回退默认+警告），
            # 与上下文预算同一处置哲学，避免拼写错误崩启动或静默失效。
            relevance_threshold=_env_positive_float(
                "API_RETRIEVAL_THRESHOLD", DEFAULT_RETRIEVAL_THRESHOLD
            ),
            query_refiner=HeuristicQueryRefiner(),
            query_rewriter=query_rewriter,
            reranker=reranker,
        )
        # H-T1 统一结构化启动日志：hybrid / lexical_only 都打，让运维
        # 一眼看出语义检索是否在线。只打模式 / provider / 维度与增强
        # 开关状态，不打印任何文件路径（旧日志打印 vector_db 绝对路径，
        # 属部署细节）。
        mode = "hybrid" if knowledge_stack.vector_enabled else "lexical_only"
        _LOGGER.info(
            "知识检索模式=%s embedding_provider=%s vector_dimension=%s "
            "query_rewrite=%s reranker=%s",
            mode,
            knowledge_stack.vector_provider,
            knowledge_stack.vector_dimension,
            knowledge_stack.rewrite_enabled,
            knowledge_stack.reranker_enabled,
        )
        # 诊断快照挂到 app.state，供 /healthz 输出（字段只含 mode /
        # provider / 维度与改写/重排开关状态，绝不含路径；lifespan 未跑
        # 或已退出时该属性不存在/为 None，/healthz 用 getattr 兜底保持
        # 现状）。挂在 try 内：图装配失败时不留「与实际不符」的快照。
        try:
            app.state.graph = CollaborativeAgentGraph(
                model=cast(ChatModel, create_deepseek_model(model_settings)),
                checkpointer=checkpointer,
                # 生产链路采用 supervisor-as-primary：Worker 作为可等待
                # 的工具调用，结果返回 supervisor 后由其整合本轮答案。
                # 不在委派处 interrupt，避免请求在子代理响应前提前结束。
                orchestration_mode="tool",
                # P0-1 上下文预算（默认值依据与权衡见
                # DEFAULT_MAX_CONTEXT_TOKENS 注释）：护栏上限而非目标
                # 填充量，env 可随时调整；裁剪设施 context.py 已就绪，
                # 不传计数器时走内置保守估算（零新依赖）。
                max_context_tokens=_env_positive_int(
                    "API_MAX_CONTEXT_TOKENS", DEFAULT_MAX_CONTEXT_TOKENS
                ),
                max_context_messages=_env_positive_int(
                    "API_MAX_CONTEXT_MESSAGES", DEFAULT_MAX_CONTEXT_MESSAGES
                ),
                # P0-4：学习记录 store 注入（None 时学习工具不注册、
                # 图行为与现状逐字节一致——既有测试零改动红线）。
                learning_store=learning_store,
                # 业务工具与授权：search_knowledge 只授给需要产出知识
                # 内容的两个 Worker（理由见模块底部 _KNOWLEDGE_TOOL_PERMISSIONS
                # 的注释）；graph_builder 会校验权限声明完整（缺工具或
                # 权限为 None 会抛 ValueError）。
                tools=[
                    knowledge_stack.tool,
                    *read_only_file_tools,
                    shell_tool,
                    *office_tools,
                ],
                tool_permissions={
                    **_KNOWLEDGE_TOOL_PERMISSIONS,
                    **_READ_ONLY_FILE_TOOL_PERMISSIONS,
                    **_SHELL_TOOL_PERMISSIONS,
                    **office_tool_permissions,
                },
                # Shell enforces its own user-selected timeout (max 120s).
                # Keep the generic executor deadline slightly above it so the
                # shell can terminate its process tree and return diagnostics.
                tool_timeouts={
                    "shell": MAX_SHELL_TIMEOUT_SECONDS + 5,
                    **office_tool_timeouts,
                },
            )
            app.state.session_store = session_store
            # D6-T3:检索服务挂到 app.state,供 /knowledge/search 路由
            # 经 get_knowledge_service 依赖注入(见 api/knowledge.py)。
            # 挂在 try 内:图装配失败时不留下指向已关闭索引的服务。
            app.state.knowledge_service = knowledge_stack.service
            # I1:知识库清单服务挂到 app.state,供 /knowledge/overview 与
            # /knowledge/documents 路由经 get_knowledge_catalog 注入(见
            # api/knowledge.py)。与 service 同挂在 try 内(同一清理语义)。
            app.state.knowledge_catalog = knowledge_stack.catalog
            app.state.retrieval_diagnostics = {
                "mode": mode,
                "embedding_provider": knowledge_stack.vector_provider,
                "vector_dimension": knowledge_stack.vector_dimension,
                "rewrite_enabled": knowledge_stack.rewrite_enabled,
                "reranker_enabled": knowledge_stack.reranker_enabled,
            }
            # P0-4/P0-6：学习记录 store 与 OCR provider 挂 app.state，
            # 供 api/learning.py 诊断端点（P3-15）与附件提取链路
            # （P2-7）依赖注入；与 knowledge_service 同挂在 try 内
            # （图装配失败时不留下指向已关闭资源的引用）。
            app.state.learning_store = learning_store
            app.state.ocr_provider = ocr_provider
            app.state.vision_provider = vision_provider
            yield
        finally:
            # 释放顺序：先关知识索引（图已不再执行，工具闭包不再被
            # 调用），再关会话库与学习记录库，最后清空状态引用。诊断快照一并清除，
            # 与「lifespan 未跑时 /healthz 不带 retrieval」语义一致。
            knowledge_stack.close()
            session_store.close()
            learning_store.close()
            app.state.graph = None
            app.state.session_store = None
            app.state.retrieval_diagnostics = None
            # D6-T3:检索服务一并清空,与「lifespan 未跑时不带知识
            # 服务」语义一致(路由经 getattr 兜底返回 503)。
            app.state.knowledge_service = None
            # I1:清单服务一并清空(与 service 同一清理语义;catalog
            # 连接已由 knowledge_stack.close 一并关闭)。
            app.state.knowledge_catalog = None
            # P0-4/P0-6：学习 store 与 OCR provider 引用清空（同一
            # 「lifespan 未跑时该属性不存在/为 None」的 getattr 兜底语义）。
            app.state.learning_store = None
            app.state.ocr_provider = None
            app.state.vision_provider = None


def create_app() -> FastAPI:
    """Create the API application."""
    app = FastAPI(lifespan=lifespan)
    app.state.chat_session_locks = {}
    install_openapi_contract(app)
    app.include_router(chat_router)
    app.include_router(stream_router)
    app.include_router(approval_router)
    app.include_router(tool_approval_router)
    app.include_router(session_router)
    app.include_router(stats_router)
    app.include_router(feedback_router)
    app.include_router(knowledge_router)
    # 六大功能 P3-15：学情诊断端点（learning.db 聚合视图）。
    app.include_router(learning_router)
    app.include_router(files_router)
    app.include_router(workspace_router)

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
        """存活探针；lifespan 装配后附带检索与 OCR 诊断（H-T1 / P0-6）。

        - lifespan 未跑（如单测直接 create_app()）或诊断未就绪：保持
          {"status": "ok"} 现状，不破坏既有探针语义与测试；
        - lifespan 跑过：附加 retrieval 字段（mode / embedding_provider /
          vector_dimension）与 ocr 字段（enabled，P0-6：图片附件识别
          能力是否在线），运维据此判断可选能力状态。诊断只含状态字段，
          绝不含任何文件路径。
        """
        diagnostics = getattr(request.app.state, "retrieval_diagnostics", None)
        if diagnostics is None:
            return {"status": "ok"}
        return {
            "status": "ok",
            "retrieval": diagnostics,
            "ocr": {
                "enabled": getattr(request.app.state, "ocr_provider", None)
                is not None
            },
        }

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
# - evaluator（评价者）：六大功能 P2-11 起**授权**（有意逆转既定默认）。
#   原决策前提是「评价系统回答时，检索证据来自其他 Agent 调用工具的
#   观察结果（消息历史中的 ToolMessage）」；但作业批改场景（功能 2）
#   的 loop 中无其他 Agent——评分依据必须由 evaluator 自己检索对齐：
#   客观题缺失标准答案时先检索佐证（answer_source 如实标 generated），
#   主观题按教材评分标准打分（零证据不得满分）。对系统回答的评价
#   （submit_evaluation）行为不变——仍可只依据消息历史中的工具观察。
#   引用校验（chunk 级真实命中比对）由核心侧确定性完成，不依赖
#   evaluator 检索。
# 若未来 supervisor 需要直接引用知识作答，在此追加角色即可（graph_builder
# 只要求权限声明完整，不限制谁可用）。
__all__ = ["create_app", "create_knowledge_search_stack"]
