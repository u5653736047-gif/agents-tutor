"""知识库检索 REST 路由(D6-T3):POST /knowledge/search。

错误码约定(与 api/feedback.py 的 _raise_error 同构):
- 请求校验失败:app 层统一 422 + invalid_request(由 Pydantic Field
  拦截在 API 层,core 的 ValueError 兜底不会触发);
- service 缺失(app.state 未装配,lifespan 未跑/已退出,属部署问题
  而非业务错误):503 + knowledge_unavailable,客户端可重试/告警;
- service.search 内部异常(索引损坏等):500 + internal_error,
  不向客户端暴露底层错误细节。
空库(索引无内容)返回空 hits 列表,不报错——与 core search 的
「无匹配返回空列表」语义一致。

D6-T5 新增文档管理端点(错误码约定同上):
- POST /knowledge/documents:上传 txt/pdf。扩展名白名单与大小上限在
  API 层拦截为 422(不依赖 core 运行时异常);loader 解析失败(空文件/
  无文本/损坏 PDF)属请求问题,同样 422;入库成功返回 201 + 文档元数据
  (document_id/source/page_count/chunk_count);
- GET /knowledge/documents:文档清单。core 当前不提供文档枚举能力
  (KnowledgeIndex 协议仅 upsert/delete/search,service 无 list 接口),
  按「缺失能力不伪造」返回空列表(见 list_documents 注释);
- DELETE /knowledge/documents/{document_id}:删除文档。core 删除幂等
  (不存在不抛错),API 层亦无存在性判断能力,故文档不存在同样返回
  204 而非 404(见 delete_document 路由注释)。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypedDict, cast

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from starlette.concurrency import run_in_threadpool

from api.schemas import (
    ApiErrorCode,
    Citation,
    ErrorDetail,
    ErrorResponse,
    KnowledgeDocumentListEntry,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentUploadResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    SearchHitDto,
)
from core.knowledge.loaders import load_pdf, load_text
from core.knowledge.models import KnowledgeChunk, KnowledgeDocument, SearchHit
from core.knowledge.service import KnowledgeService


class _DocumentEntry(TypedDict):
    """API 层文档注册表条目(仅元数据,不含内容)。"""

    document_id: str
    source: str
    page_count: int | None
    chunk_count: int | None

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
}
# SearchHitDto.summary 的截断上限(字符数):chunk 内容可能很长,对外
# 只暴露摘要,防止单条命中撑爆响应体;超过上限截断并追加省略号。
SUMMARY_MAX_LENGTH = 200
# D6-T5 上传限制:扩展名白名单是服务端校验(文件名小写后缀;浏览器/
# 客户端可伪造 content-type,白名单以扩展名为准);大小上限在逐块读取
# 时累计拦截,不能信 Content-Length(客户端可谎报),超限即 422。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = frozenset({".pdf", ".txt"})


def _raise_error(status_code: int, error_code: ApiErrorCode, message: str) -> NoReturn:
    """与 api/feedback._raise_error 同构:抛出标准 ErrorResponse 体。"""
    detail = ErrorDetail(error_code=error_code, message=message)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def get_knowledge_service(request: Request) -> KnowledgeService:
    """从 app.state 取知识检索服务(lifespan 装配,见 app.py)。

    为什么用 getattr 兜底而不是直接访问属性:单测直接 create_app()
    不跑 lifespan 时该属性不存在,直接访问会抛 AttributeError 变成
    500。装配缺失属部署问题而非业务错误,返回稳定的 503 +
    knowledge_unavailable,客户端可据此重试/告警。
    """
    service = getattr(request.app.state, "knowledge_service", None)
    if service is None:
        _raise_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ApiErrorCode.KNOWLEDGE_UNAVAILABLE,
            "Knowledge search is unavailable.",
        )
    # mypy:getattr 返回 Any,用鸭子契约(search 可调用)校验装配类型,
    # cast 回 KnowledgeService 避免 no-any-return;测试替身(仅实现
    # search 的桩)同样通过。
    if not callable(getattr(service, "search", None)):
        _raise_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ApiErrorCode.KNOWLEDGE_UNAVAILABLE,
            "Knowledge search is unavailable.",
        )
    return cast(KnowledgeService, service)


def _to_dto(hit: SearchHit) -> SearchHitDto:
    """core SearchHit → API SearchHitDto。

    - summary:由 chunk 内容截断生成(上限 SUMMARY_MAX_LENGTH 字符,
      超出加省略号),不返回 chunk 全文;
    - citation:core 与 API 的 Citation 字段同名同义(document_id /
      source / page / chunk_id),按 chat.py 的 _api_citations 同一
      方式 model_dump 后逐项 validate 透传(逻辑 source 已在 core
      侧校验,不泄漏文件系统路径);
    - score:原样透传(非负,core 侧保证)。
    """
    content = hit.chunk.content
    summary = content[:SUMMARY_MAX_LENGTH]
    if len(content) > SUMMARY_MAX_LENGTH:
        summary += "…"
    return SearchHitDto(
        summary=summary,
        citation=Citation.model_validate(hit.citation.model_dump(mode="json")),
        score=hit.score,
    )


def _store_uploaded_document(
    data: bytes,
    basename: str,
    *,
    document_id: str,
    source_label: str,
    service: KnowledgeService,
) -> tuple[list[KnowledgeDocument], list[KnowledgeChunk]]:
    """写临时文件 → core loader 解析 → service 幂等替换入库(同步,线程池内跑)。

    - loader 只接受文件路径(load_text / load_pdf 的 path 参数),上传
      字节必须先落临时文件;TemporaryDirectory 在任意异常路径(解析失败/
      入库失败)都会整体清理,不会泄漏临时文件;
    - document_id / source_label 由 API 层显式传入:loader 默认按
      「文件名 stem + 绝对路径哈希」生成 ID,而临时文件路径每次随机,
      默认 ID 不稳定,重复上传无法命中替换语义——API 以「文件名 stem」
      为逻辑标识(重传同名文件 = 更新同一文档,复用 core 替换语义);
    - loader 的 ValueError(空文件/无文本/损坏 PDF)原样上抛,由路由
      映射为 422:文件内容不可解析属请求问题,不是服务故障。
    """
    ext = Path(basename).suffix.lower()
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 临时文件名固定(与上传文件名解耦):document_id / source 都是
        # 显式传入,临时文件名只用于满足 loader 的路径参数,固定名还能
        # 避开 basename 里的非法文件名字符。
        tmp_path = Path(tmp_dir) / f"upload{ext}"
        tmp_path.write_bytes(data)
        if ext == ".pdf":
            documents = load_pdf(
                tmp_path, document_id=document_id, source_label=source_label
            )
        else:
            documents = load_text(
                tmp_path, document_id=document_id, source_label=source_label
            )
        chunks = service.add_documents(documents)
    return documents, chunks


@router.post(
    "/search",
    response_model=KnowledgeSearchResponse,
    responses=ERROR_RESPONSES,
)
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
) -> KnowledgeSearchResponse:
    """检索知识库,返回命中的脱敏摘要与逻辑引用。

    - 空库返回空 hits,不报错(与 core 语义一致);
    - top_k 已被 Pydantic 拦截在 1-10,core 的 ValueError 兜底不会
      触发(见 KnowledgeSearchRequest 注释);
    - service.search 内部异常统一映射 500 internal_error,不泄底层
      细节(与 api/feedback 的存储异常处理同构)。
    """
    try:
        # review 修正:同步核心调用走 run_in_threadpool,避免阻塞事件循环
        # (chat.py / approvals.py / stream.py 对一切同步核心调用的既有约定)。
        hits = await run_in_threadpool(
            service.search,
            payload.query,
            top_k=payload.top_k,
        )
    except Exception:  # noqa: BLE001 - 服务边界只暴露稳定错误码,不泄底层细节
        _raise_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ApiErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
        )
    return KnowledgeSearchResponse(hits=[_to_dto(hit) for hit in hits])


@router.post(
    "/documents",
    response_model=KnowledgeDocumentUploadResponse,
    responses=ERROR_RESPONSES,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    file: UploadFile,
    request: Request,
    service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
) -> KnowledgeDocumentUploadResponse:
    """上传 txt/pdf 文档入库(幂等替换),返回文档元数据回执。

    - 扩展名白名单 / 大小上限 / 空文件都在 API 层拦截为 422,不依赖
      core 运行时异常(大小在逐块读取时累计,不能信 Content-Length);
    - document_id = 上传文件名 stem,source = 上传文件名(逻辑标识,
      不泄漏文件系统路径):重传同名文件 → 同一 document_id → core
      替换语义,旧内容被新内容覆盖;
    - loader 解析失败(空文件/无文本/损坏 PDF)映射 422 invalid_request
      (内容不可解析属请求问题);入库内部异常映射 500 internal_error,
      不泄底层细节。
    """
    # 文件名可能被浏览器/客户端带上路径前缀(如 "C:\\fakepath\\x.txt"
    # 或 "/tmp/x.txt"),先剥掉一切分隔符得到纯文件名,再取小写后缀。
    basename = (file.filename or "").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    ext = Path(basename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        _raise_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ApiErrorCode.INVALID_REQUEST,
            "Unsupported file type.",
        )
    # 逐块读取并累计大小(上限 10MB,一次性读入内存可接受):不能信
    # Content-Length(客户端可谎报),超限立即 422,不再继续读。
    data = bytearray()
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        data.extend(block)
        if len(data) > MAX_UPLOAD_BYTES:
            _raise_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                ApiErrorCode.INVALID_REQUEST,
                "File is too large.",
            )
    if not data:
        _raise_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ApiErrorCode.INVALID_REQUEST,
            "File is empty.",
        )
    try:
        # 同步核心调用(写临时文件 + loader 解析 + 入库)走 run_in_threadpool,
        # 与 search 端点的既有约定一致,避免阻塞事件循环。
        documents, chunks = await run_in_threadpool(
            _store_uploaded_document,
            bytes(data),
            basename,
            document_id=Path(basename).stem,
            source_label=basename,
            service=service,
        )
    except ValueError:
        # loader 解析失败(空文件/无文本/损坏 PDF)属请求问题 → 422;
        # service.add_documents 的 ValueError(同批重复页)在 API 层生成
        # 的输入下不可达,若发生也归入此分支,不泄底层细节。
        _raise_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ApiErrorCode.INVALID_REQUEST,
            "The uploaded file could not be parsed.",
        )
    except Exception:  # noqa: BLE001 - 服务边界只暴露稳定错误码,不泄底层细节
        _raise_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ApiErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
        )
    pages = {document.page for document in documents if document.page is not None}
    response = KnowledgeDocumentUploadResponse(
        # loader 保证返回非空列表(空文件会抛 ValueError 走 422 分支)。
        document_id=documents[0].document_id,
        source=documents[0].source,
        page_count=len(pages) if pages else None,
        chunk_count=len(chunks),
    )
    # D6-T5 review 修正:core 无文档枚举能力,API 层注册表记录本次上传
    # (GET /knowledge/documents 由此返回;删除时同步移除)。
    _document_registry(request)[documents[0].document_id] = {
        "document_id": documents[0].document_id,
        "source": documents[0].source,
        "page_count": response.page_count,
        "chunk_count": response.chunk_count,
    }
    return response


@router.get(
    "/documents",
    response_model=KnowledgeDocumentListResponse,
    responses=ERROR_RESPONSES,
)
async def list_documents(
    request: Request,
) -> KnowledgeDocumentListResponse:
    """列出通过 API 上传的文档元数据。

    core 的 KnowledgeIndex 协议不提供文档枚举能力(仅 upsert /
    delete_document / search),因此 API 层维护进程内注册表
    (_document_registry):只登记经 POST /knowledge/documents 上传的
    文档——由 ingest_books 等脚本直接写入索引的文档不在列表内
    (core 扩展清单能力后可与注册表合并)。注册表挂 app.state(随
    app 生命周期,测试各 app 实例隔离)。
    """
    registry = _document_registry(request)
    return KnowledgeDocumentListResponse(
        documents=[
            KnowledgeDocumentListEntry(
                document_id=entry["document_id"],
                source=entry["source"],
                page_count=entry["page_count"],
                chunk_count=entry["chunk_count"],
            )
            for entry in registry.values()
        ]
    )


def _document_registry(request: Request) -> dict[str, _DocumentEntry]:
    """惰性取/建文档注册表(app.state.knowledge_documents)。"""
    registry = getattr(request.app.state, "knowledge_documents", None)
    if not isinstance(registry, dict):
        registry = {}
        request.app.state.knowledge_documents = registry
    return registry


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERROR_RESPONSES,
)
async def delete_document(
    document_id: str,
    request: Request,
    service: Annotated[KnowledgeService, Depends(get_knowledge_service)],
) -> None:
    """删除文档(幂等:文档不存在也返回 204,不报 404)。

    core 的 KnowledgeService.delete_document 是幂等删除(不存在不抛错),
    且 API 层没有文档存在性查询能力(原因见 list_documents 注释),无法
    区分「存在/不存在」。按 core 语义,删除不存在的文档同样返回 204——
    重复删除/清理任务幂等安全;待 core 提供清单/存在性能力后再增加
    404 区分。注册表同步移除该条目。
    """
    await run_in_threadpool(service.delete_document, document_id)
    _document_registry(request).pop(document_id, None)
