"""Synchronous chat REST route backed by the collaborative graph."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request, status
from langchain_core.messages import AIMessage, BaseMessage
from starlette.concurrency import run_in_threadpool

from api.schemas import (
    AgentRole,
    ApiErrorCode,
    ChatRequest,
    ChatResponse,
    Citation,
    ErrorResponse,
    HandoffRequest,
    Message,
    MessageRole,
    PendingHandoff,
    RunError,
    RunEvent,
    StreamEventType,
    TaskPlan,
    TaskPlanStatus,
    TaskPlanStep,
    TaskResult,
    WorkerAgentRole,
)
from api.schemas import (
    ErrorCode as ApiRunErrorCode,
)
from api.sessions import current_user_id
from core.events import EventType
from core.events import RunError as CoreRunError
from core.events import RunEvent as CoreRunEvent
from core.graph_builder import CollaborativeAgentGraph
from core.sessions import SessionStore
from core.state import AgentState, PendingHandoffApproval
from core.state import TaskPlan as CoreTaskPlan
from core.state import TaskStepResult as CoreTaskStepResult
from core.state import message_references as core_message_references

router = APIRouter(tags=["chat"])
CHAT_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}
EVENT_TYPE_MAP = {
    EventType.AGENT_STARTED: StreamEventType.THINKING,
    EventType.TOOL_STARTED: StreamEventType.TOOL_CALL,
    EventType.TOOL_COMPLETED: StreamEventType.TOOL_RESULT,
    EventType.AGENT_COMPLETED: StreamEventType.MESSAGE_END,
    EventType.AGENT_SWITCHED: StreamEventType.AGENT_SWITCH,
    EventType.RUN_FAILED: StreamEventType.ERROR,
    EventType.RUN_COMPLETED: StreamEventType.DONE,
}
PENDING_RESUME_ERROR_PREFIX = "存在待恢复执行，请先调用 "


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def session_lock(
    request: Request, session_id: str, user_id: str | None
) -> asyncio.Lock:
    locks = cast(
        dict[tuple[str | None, str], asyncio.Lock], request.app.state.chat_session_locks
    )
    key = (user_id, session_id)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _public_agent(value: object) -> AgentRole | None:
    if not isinstance(value, str):
        return None
    try:
        return AgentRole(value)
    except ValueError:
        return None


def _public_task_plan(plan: object) -> TaskPlan | None:
    """core TaskPlan → 公开契约 TaskPlan（字段一一对应；整体类型不符 → None）。

    core 与 API 的 TaskPlan / TaskPlanStep 字段同名同义，这里按字段
    逐项映射；core 的 WorkerAgentRole 是 AgentRole 的 Literal 别名、
    API 侧是独立枚举（值字符串一致），target_agent 显式按值转换，
    status 同理（core StrEnum 与 api Enum 值一致）。注意：字段级非法
    值（如未知枚举）会抛 ValueError——core 模型 extra="forbid" + 类型
    校验保证 CoreTaskPlan 实例字段必然合法，isinstance 入口已挡掉
    脏 dict，故不做字段级降级（与 _public_event 的策略一致）。
    """
    if not isinstance(plan, CoreTaskPlan):
        return None
    return TaskPlan(
        steps=[
            TaskPlanStep(
                sequence=step.sequence,
                description=step.description,
                target_agent=WorkerAgentRole(step.target_agent.value),
            )
            for step in plan.steps
        ],
        current_step_index=plan.current_step_index,
        status=TaskPlanStatus(plan.status.value),  # core StrEnum 与 api Enum 值一致
    )


def _public_task_results(results: object) -> list[TaskResult] | None:
    """core TaskStepResult 列表 → 公开契约 TaskResult 列表（缺失/类型不符 → None）。

    逐项 isinstance 防御：列表中出现非 core TaskStepResult 的脏项时
    跳过而不是整体失败；全部跳过（或空列表）时归一化为 None，与
    「无结果就不携带」的契约一致。error_code 与 target_agent 按值
    转换到 API 侧枚举（core/API 枚举值字符串一致，见 _public_event）。
    """
    if not isinstance(results, list):
        return None
    public: list[TaskResult] = []
    for item in results:
        if not isinstance(item, CoreTaskStepResult):
            continue
        public.append(
            TaskResult(
                step_sequence=item.step_sequence,
                target_agent=WorkerAgentRole(item.target_agent.value),
                success=item.success,
                output=item.output,
                error_code=(
                    ApiRunErrorCode(item.error_code.value)
                    if item.error_code is not None
                    else None
                ),
            )
        )
    return public or None


def _safe_created_at(message: BaseMessage) -> datetime | None:
    created_at = getattr(message, "created_at", None)
    return created_at if isinstance(created_at, datetime) else None


def _is_answer_message(message: BaseMessage) -> bool:
    """是否为一条「作答消息」：助手输出、无工具调用、纯文本内容。

    最终响应 message 与 references 都按这个判定找消息，保证两者
    指向同一轮作答（引用必须与回答内容对应）。
    """
    return (
        isinstance(message, AIMessage)
        and not message.tool_calls
        and isinstance(message.content, str)
    )


def _final_assistant_message(
    state: AgentState, previous_message_count: int
) -> Message | None:
    agent = _public_agent(state.get("current_agent"))
    messages = state.get("messages", [])
    for message in reversed(messages[previous_message_count:]):
        if not _is_answer_message(message):
            continue
        content = message.content
        if not isinstance(content, str):
            # 防御性收窄：_is_answer_message 内部的 isinstance 判断不会
            # 跨函数传播类型收窄，这里显式重复一次，让 mypy 确认 content
            # 是纯文本（运行时必然成立，与 api/sessions._public_message
            # 「非纯文本内容不对外暴露」的公开口径保持一致）。
            continue
        return Message(
            role=MessageRole.ASSISTANT,
            content=content,
            agent=agent,
            created_at=_safe_created_at(message),
        )
    return None


def _api_citations(message: BaseMessage) -> list[Citation] | None:
    """把 core 消息元数据里的引用转成 API 契约的 Citation 列表。

    core 与 API 的 Citation 字段同名同义（document_id / source /
    page / chunk_id），core 侧已做过逻辑来源与字段校验，这里按
    model_dump 结果逐项 validate 直接透传，不需要字段映射。core
    返回空列表（元数据有 references 键但内容不可解析的脏数据）时
    归一化为 None——与「无引用就不携带」的契约一致。
    """
    citations = core_message_references(message)
    if not citations:
        return None
    return [
        Citation.model_validate(citation.model_dump(mode="json"))
        for citation in citations
    ]


def _response_references(
    state: AgentState, previous_message_count: int
) -> list[Citation] | None:
    """本轮响应要携带的引用列表（口径与 S2-T4 的「按作答消息渲染」一致）。

    验收要求「最终回答携带 references 元数据且引用来自真实检索」，
    而检索证据挂在 worker 的作答消息上、supervisor 的聚合回答本身
    不带引用（S2-T4 语义：引用跟随使用证据作答的消息）。采用两级口径：

    1. 优先取本轮最新作答消息（与响应 message 同一条消息）自身的
       引用——严格按消息，引用与回答内容一一对应；
    2. 若最新作答消息无引用（典型场景：supervisor 聚合回答），回退
       扫描本轮更早的作答消息（从新到旧），取最近一条带引用的——
       聚合回答的内容正是对这些 worker 检索作答的汇总，最近一次
       检索的引用与回答内容仍然对应，同时保证验收场景引用可见；
    3. 本轮没有任何作答消息（如 run_error 提前终止）或均无引用 →
       None，与 core「无引用就不携带」的零命中语义一致。

    只扫描本轮新增消息（previous_message_count 之后），历史轮次的
    引用不跨轮次渲染。
    """
    messages = state.get("messages", [])
    new_messages = messages[previous_message_count:]
    final_message: BaseMessage | None = None
    for message in reversed(new_messages):
        if _is_answer_message(message):
            final_message = message
            break
    if final_message is None:
        return None
    references = _api_citations(final_message)
    if references is not None:
        return references
    for message in reversed(new_messages):
        if _is_answer_message(message):
            references = _api_citations(message)
            if references is not None:
                return references
    return None


def _previous_sequence(state: AgentState | None) -> int:
    if state is None:
        return -1
    return max(
        (
            event.sequence
            for event in state.get("events", [])
            if isinstance(event, CoreRunEvent)
        ),
        default=-1,
    )


def _previous_message_count(state: AgentState | None) -> int:
    return 0 if state is None else len(state.get("messages", []))


def _public_event(event: CoreRunEvent) -> RunEvent | None:
    event_type = EVENT_TYPE_MAP.get(event.event_type)
    if event_type is None:
        return None
    error_code = None
    if event.error_code is not None:
        error_code = ApiRunErrorCode(event.error_code.value)
    return RunEvent(
        event_type=event_type,
        sequence=event.sequence,
        session_id=event.session_id,
        agent=_public_agent(event.agent),
        tool_name=event.tool_name,
        success=event.success,
        duration_ms=event.duration_ms,
        error_code=error_code,
        plan_step_sequence=event.plan_step_sequence,
        degraded=event.degraded,
    )


def _public_events(events: Iterable[object], sequence: int) -> list[RunEvent]:
    return [
        public_event
        for event in events
        if isinstance(event, CoreRunEvent)
        and event.sequence > sequence
        and (public_event := _public_event(event)) is not None
    ]


def _public_run_error(error: object) -> RunError | None:
    if not isinstance(error, CoreRunError):
        return None
    return RunError(
        error_code=ApiRunErrorCode(error.error_code.value),
        message="The request could not be completed.",
        agent=_public_agent(error.agent),
    )


def _public_pending_handoff(pending: object) -> PendingHandoff | None:
    if not isinstance(pending, PendingHandoffApproval):
        return None
    return PendingHandoff(
        interrupt_id=pending.interrupt_id,
        request=HandoffRequest(
            target_agent=WorkerAgentRole(pending.request.target_agent.value),
            task_content=pending.request.task_content,
            plan_step_sequence=pending.request.plan_step_sequence,
        ),
    )


def _ensure_session(session_store: SessionStore, session_id: str, user_id: str | None) -> None:
    if any(
        record.session_id == session_id
        for record in session_store.list_sessions(user_id=user_id, include_archived=True)
    ):
        return
    try:
        session_store.create_session(session_id, user_id=user_id)
    except ValueError:
        if not any(
            record.session_id == session_id
            for record in session_store.list_sessions(user_id=user_id, include_archived=True)
        ):
            raise


async def pending_handoff_for_session(
    graph: CollaborativeAgentGraph, session_id: str, user_id: str | None
) -> PendingHandoff | None:
    pending = await run_in_threadpool(graph.get_pending_handoff, session_id, user_id)
    return _public_pending_handoff(pending)


def session_busy_response(session_id: str, message: str) -> ChatResponse:
    return ChatResponse(
        session_id=session_id,
        run_error=RunError(
            error_code=ApiErrorCode.SESSION_BUSY,
            message=message,
        ),
    )


async def chat_response_for_state(
    graph: CollaborativeAgentGraph,
    state: AgentState,
    session_id: str,
    user_id: str | None,
    previous_state: AgentState | None,
) -> ChatResponse:
    """Convert one completed graph transition into the public chat contract."""
    previous_count = _previous_message_count(previous_state)
    return ChatResponse(
        session_id=session_id,
        message=_final_assistant_message(state, previous_count),
        references=_response_references(state, previous_count),
        task_plan=_public_task_plan(state.get("task_plan")),
        task_results=_public_task_results(state.get("task_results")),
        events=_public_events(state.get("events", []), _previous_sequence(previous_state)),
        run_error=_public_run_error(state.get("run_error")),
        pending_handoff=await pending_handoff_for_session(graph, session_id, user_id),
        current_agent=_public_agent(state.get("current_agent")),
    )


@router.post("/chat", response_model=ChatResponse, responses=CHAT_ERROR_RESPONSES)
async def chat(
    payload: ChatRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
) -> ChatResponse:
    """Run one synchronous collaboration turn in a worker thread."""
    graph = _graph(request)
    session_store = _session_store(request)
    active_session_lock = session_lock(request, payload.session_id, user_id)
    if active_session_lock.locked():
        return session_busy_response(
            payload.session_id,
            "Another request is already running for this session.",
        )

    async with active_session_lock:
        await run_in_threadpool(_ensure_session, session_store, payload.session_id, user_id)
        previous_state = await run_in_threadpool(
            graph.get_state, payload.session_id, user_id
        )
        try:
            state = await run_in_threadpool(
                graph.run, payload.message, payload.session_id, user_id
            )
        except RuntimeError as error:
            pending_handoff = await pending_handoff_for_session(
                graph, payload.session_id, user_id
            )
            if pending_handoff is not None or str(error).startswith(
                PENDING_RESUME_ERROR_PREFIX
            ):
                return ChatResponse(
                    session_id=payload.session_id,
                    run_error=RunError(
                        error_code=ApiErrorCode.SESSION_BUSY,
                        message="The session is waiting for a pending operation.",
                    ),
                    pending_handoff=pending_handoff,
                )
            return ChatResponse(
                session_id=payload.session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )
        except Exception:  # noqa: BLE001 - graph boundary exposes only stable error data
            return ChatResponse(
                session_id=payload.session_id,
                run_error=RunError(
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                    message="The request could not be completed.",
                ),
            )

        return await chat_response_for_state(
            graph,
            state,
            payload.session_id,
            user_id,
            previous_state,
        )
