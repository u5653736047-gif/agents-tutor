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
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from api.schemas import (
    ApiErrorCode,
    Citation,
    ErrorDetail,
    ErrorResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    SearchHitDto,
)
from core.knowledge.models import SearchHit
from core.knowledge.service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
}
# SearchHitDto.summary 的截断上限(字符数):chunk 内容可能很长,对外
# 只暴露摘要,防止单条命中撑爆响应体;超过上限截断并追加省略号。
SUMMARY_MAX_LENGTH = 200


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
