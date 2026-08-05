"""学习进度基础统计 REST 路由(D6-T7)。

只读聚合,不写任何状态:统计完全建立在既有的 SessionStore(会话
元数据)与 graph.get_history(消息历史)之上,core 零改动。

统计口径(与既有 API 对齐):
- 用户隔离:list_sessions(user_id=...) 按 user_key 过滤,越权会话
  天然不计入(与 api/sessions.py 同一命名空间口径);
- 匿名用户:无 X-User-Id 时统计匿名命名空间("none"),与 sessions
  路由的匿名会话先例一致,不返回 422;
- 角色识别:复用 api/sessions._safe_agent(角色元数据优先、name
  回退、最终降级 None),「回答」判定为无工具调用的纯文本 AIMessage
  (与 api/chat._is_answer_message 同构,工具中间输出不算回答);
- 最近活动时间:取所有会话 created_at 的最大值。langchain-core 的
  BaseMessage 没有 created_at 字段(api/sessions._safe_created_at
  的 getattr 兜底恒为 None),消息级时间在现有持久化里不可用,故
  诚实降级为「会话创建时间」,不伪造消息时间;
- graph 缺失降级:app.state.graph 只在 lifespan 装配(测试直接
  create_app() 不跑 lifespan)。SessionStore 只存会话元数据、不存
  消息,消息只能从 graph.get_history 读取——graph 缺失时统计降级
  为「只有会话数」,message_count=0 / agent_answer_counts={},
  不报错(与 /healthz 的 getattr 兜底同一容忍哲学)。

性能说明:每个会话一次 get_history(N+1)。教学项目会话规模小、
checkpoint 读取在本地 SQLite,可接受;大规模部署应改为批量统计
(如直接在 checkpoint 库上做 SQL 聚合),本期不做。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langchain_core.messages import AIMessage, BaseMessage
from starlette.concurrency import run_in_threadpool

from api.schemas import ApiErrorCode, ErrorDetail, ErrorResponse, StatsOverview
from api.sessions import _safe_agent, current_user_id
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionStore

router = APIRouter(prefix="/stats", tags=["stats"])
STATS_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
}


def _raise_error(status_code: int, error_code: ApiErrorCode, message: str) -> NoReturn:
    detail = ErrorDetail(error_code=error_code, message=message)
    raise HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph | None:
    """取 app.state.graph;lifespan 未装配(如单测直接 create_app())时为 None。

    为什么容忍缺失而不是像 sessions/chat 路由一样强取:统计是只读
    聚合,graph 缺失时降级为「只有会话数」而不是 500(降级口径见
    模块注释与 stats_overview)。
    """
    return cast(CollaborativeAgentGraph | None, getattr(request.app.state, "graph", None))


def _is_answer_message(message: BaseMessage) -> bool:
    """是否为一条「助手回答」:无工具调用的纯文本 AIMessage。

    与 api/chat._is_answer_message 同构,保证「回答」的判定口径一致:
    带 tool_calls 的模型中间输出与工具消息不算回答。
    """
    return (
        isinstance(message, AIMessage)
        and not message.tool_calls
        and isinstance(message.content, str)
    )


@router.get("/overview", response_model=StatsOverview, responses=STATS_ERROR_RESPONSES)
async def stats_overview(
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> StatsOverview:
    """返回当前用户的学习进度基础统计(只读聚合)。"""
    try:
        session_store = _session_store(request)
        # include_archived=True:进度是历史累计,归档会话(已完成的对话)
        # 应继续计入,避免用户归档后学习进度「清零」的困惑。
        records = await run_in_threadpool(
            session_store.list_sessions, user_id, include_archived=True
        )
        graph = _graph(request)
        message_count = 0
        answer_counts: Counter[str] = Counter()
        last_activity_at: datetime | None = None
        for record in records:
            # 会话创建即活动:langchain-core 的 BaseMessage 无 created_at
            # 字段,消息级时间在现有持久化里不可用(见模块注释),最近
            # 活动时间统一取会话 created_at 的最大值。
            if last_activity_at is None or record.created_at > last_activity_at:
                last_activity_at = record.created_at
            if graph is None:
                # 降级:消息只能从 graph 读,graph 缺失时本会话贡献 0 消息
                # (SessionStore 不存消息,见模块注释)。
                continue
            messages = await run_in_threadpool(
                graph.get_history, record.session_id, user_id
            )
            message_count += len(messages)
            for message in messages:
                if _is_answer_message(message):
                    agent = _safe_agent(message)
                    if agent is not None:
                        # _safe_agent 口径:识别不出角色的回答(降级「助手」)
                        # 不计入任何角色的分布,保证前端柱状条总和可解释。
                        answer_counts[agent.value] += 1
        return StatsOverview(
            session_count=len(records),
            message_count=message_count,
            agent_answer_counts=dict(answer_counts),
            last_activity_at=(
                last_activity_at.isoformat() if last_activity_at is not None else None
            ),
        )
    except Exception:  # noqa: BLE001 - 聚合失败统一收敛为稳定 500,不泄露内部细节
        _raise_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ApiErrorCode.INTERNAL_ERROR,
            "The request could not be completed.",
        )
