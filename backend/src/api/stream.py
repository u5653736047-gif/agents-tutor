"""SSE 原生流式聊天端点(D1-T1,D1-T3 断线重连与消息补发)。

与 POST /chat 的差异:POST /chat 在一个请求内等待完整 run 结束并返回
最终契约;本端点消费 LangGraph 的 messages/custom 双通道,正文 token
与安全运行事件按 sequence 增量通过 SSE 推送,最后以 message_end
(携带权威全文)+ done 收尾。没有原生 stream() 的兼容图仍使用 checkpoint
轮询事件,但生产图不走该降级路径。

D1-T3 断线重连与消息补发:客户端可在查询参数传 from_sequence
(上次收到的最新公开 sequence)。正数游标只具有「续传」语义：最近
一轮已结束时回放并补权威全文；仍在执行或状态不完整时返回一个空的
SSE 重试响应，绝不把同一输入启动为新 run。只有 from_sequence=0
才代表一条新用户消息并获准启动执行。

过程事件约定(与 api/chat.py 同口径):
- tool_call / tool_result 携带 core 已有界、脱敏的输入/输出摘要;
- thinking 是按 Agent 角色映射的阶段文案；reasoning 只转发 provider
  显式返回的 reasoning/thinking 字段，不从普通正文推断或伪造;
- message_end 的 content 是最终消息全文(与 POST /chat 的
  ChatResponse.message.content 同源)。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import AIMessageChunk
from starlette.concurrency import run_in_threadpool

from api.attachments import compose_message_with_attachments
from api.chat import (
    PENDING_RESUME_ERROR_PREFIX,
    _ensure_session_with_title,
    _final_assistant_message,
    _previous_message_count,
    _previous_sequence,
    _public_agent,
    _public_event,
    _public_events,
    _public_grading,
    _public_run_error,
    _response_references,
    _run_graph_turn,
    _workspace_call_kwargs,
    pending_handoff_for_session,
    pending_tool_approval_for_session,
    session_busy_response,
    session_lock,
)
from api.schemas import (
    ApiErrorCode,
    ChatRequest,
    RunEvent,
    StreamEvent,
    StreamEventType,
)
from api.sessions import current_user_id
from core.events import EventType
from core.events import RunEvent as CoreRunEvent
from core.graph_builder import CollaborativeAgentGraph
from core.ocr import OcrProvider
from core.sessions import SessionRecord, SessionStore
from core.state import AgentState, ToolApprovalDecision

router = APIRouter(tags=["chat"])
_LOGGER = logging.getLogger("api.stream")


def _ocr_provider_from_request(request: Request) -> object | None:
    """从 app.state 取 OCR provider；request 无 app 时返回 None。

    防御性访问：生产 Request 恒有 app；测试替身（只实现
    is_disconnected 的 _ConnectedRequest 等）没有 app 属性——此时
    OCR 按 None 处理（图片附件走友好提示降级），不击穿替身路径。
    """
    app = getattr(request, "app", None)
    if app is None:
        return None
    return getattr(app.state, "ocr_provider", None)

# SSE 轮询间隔:core 是同步 run,事件在 checkpoint 里逐步落盘,
# 生成器以固定间隔轮询 get_state 拿增量(50ms 对事件级推送足够)。
_POLL_INTERVAL_SECONDS = 0.05
# SSE 心跳间隔:事件稀疏的长 run 期间,定期发注释帧
# (": keepalive\n\n")防止中间代理因空闲断开;心跳不携带任何数据。
_KEEPALIVE_INTERVAL_SECONDS = 15.0

_AGENT_PROGRESS_SUMMARIES = {
    "supervisor": "正在分析问题并规划协作",
    "teaching_assistant": "正在设计教学与答疑方案",
    "learning_assistant": "正在梳理知识点与讲解路径",
    "evaluator": "正在检查答案与学习效果",
}


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _stream_event_from_run_event(event: RunEvent, session_id: str) -> StreamEvent:
    """把可回放 RunEvent 一一映射成实时 StreamEvent。"""
    content: str | None
    if event.event_type is StreamEventType.THINKING:
        # AgentRole 是 str 枚举,f-string 直接格式化会输出
        # "AgentRole.SUPERVISOR",取 .value 得到 "supervisor"。
        agent_name = event.agent.value if event.agent is not None else None
        content = event.content or _AGENT_PROGRESS_SUMMARIES.get(
            agent_name or "",
            "正在处理当前任务",
        )
    else:
        content = event.content
    return StreamEvent(
        event_type=event.event_type,
        sequence=event.sequence,
        session_id=event.session_id or session_id,
        run_id=event.run_id,
        agent=event.agent,
        tool_name=event.tool_name,
        tool_call_id=event.tool_call_id,
        parent_tool_call_id=event.parent_tool_call_id,
        input_summary=event.input_summary,
        output_summary=event.output_summary,
        success=event.success,
        duration_ms=event.duration_ms,
        error_code=event.error_code,
        plan_step_sequence=event.plan_step_sequence,
        content=content,
        output_stream=event.output_stream,
        message_id=event.message_id,
        is_delta=event.is_delta,
    )


def _sse_frame(event: StreamEvent) -> str:
    """序列化一条 SSE 帧:`data: {json}\\\\n\\\\n`(ensure_ascii=False 与
    Starlette JSONResponse 的中文输出保持一致)。"""
    return f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"


def _state_error_event(
    state: AgentState,
    sequence: int,
    session_id: str,
) -> StreamEvent | None:
    """把 checkpoint 中的运行失败转换为脱敏 SSE 错误事件。"""
    run_error = _public_run_error(state.get("run_error"))
    if run_error is None:
        return None
    return StreamEvent(
        event_type=StreamEventType.ERROR,
        sequence=sequence,
        session_id=session_id,
        run_id=state.get("run_id"),
        agent=run_error.agent,
        error_code=run_error.error_code,
    )


async def _error_event_for(
    graph: CollaborativeAgentGraph,
    session_id: str,
    sequence: int,
    error: Exception,
    user_id: str | None,
    run_id: str | None = None,
) -> StreamEvent:
    """把后台 run 的异常映射为脱敏 error 事件(与 POST /chat 分类一致)。

    - RuntimeError 且会话存在待恢复 handoff(或消息以
      PENDING_RESUME_ERROR_PREFIX 开头)→ session_busy;
    - 其余异常 → internal_error。异常正文绝不进入事件(脱敏)。
    """
    if isinstance(error, RuntimeError):
        pending = await pending_handoff_for_session(graph, session_id, user_id)
        pending_tool = await pending_tool_approval_for_session(
            graph,
            session_id,
            user_id,
        )
        if (
            pending is not None
            or pending_tool is not None
            or str(error).startswith(PENDING_RESUME_ERROR_PREFIX)
        ):
            return StreamEvent(
                event_type=StreamEventType.ERROR,
                sequence=sequence,
                session_id=session_id,
                run_id=run_id,
                error_code=ApiErrorCode.SESSION_BUSY,
            )
    return StreamEvent(
        event_type=StreamEventType.ERROR,
        sequence=sequence,
        session_id=session_id,
        run_id=run_id,
        error_code=ApiErrorCode.INTERNAL_ERROR,
    )


async def _cancel_background_task(task: asyncio.Task[AgentState]) -> None:
    """取消后台 run 任务并等待其结束,防止生成器退出后任务泄漏。

    线程池里的同步 run 无法真正中断,但任务对象会被标记取消、
    await 会返回,不会留下悬挂的 asyncio 任务。注意:锁的持有方
    (生成器)必须等到线程自然结束才退出,见 _stream_events 断连路径。
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, RuntimeError):
        await task


async def _wait_for_run(background_task: asyncio.Task[Any]) -> None:
    """等待后台 run 自然完成(不取消,不取结果)。

    线程池里的同步 run 无法被 asyncio 取消:断连后若直接退出生成器,
    会话锁会提前释放,后续请求可能与仍在运行的旧 run 并发写同一会话
    (与「同会话至多一个 run」语义冲突)。因此断连路径等待 run 完成
    再退出——锁持有到 run 结束,代价是锁占用时间等于剩余 run 时长
    (客户端已断开,不产帧,仅占锁)。
    """
    try:
        await asyncio.shield(background_task)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 图边界异常只需记录(客户端已断开)
        # run 异常无需向已断开的客户端推送,这里只等它结束;
        # 异常正文不落日志正文之外(稳定错误码口径,见 _error_event_for)。
        _LOGGER.info("stream disconnected: background run finished with error")


def _text_delta(chunk: object) -> str:
    """只提取回答正文；reasoning 由独立通道处理。"""
    if not isinstance(chunk, AIMessageChunk):
        return ""
    content = chunk.content
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, Mapping):
            continue
        if block.get("type") not in {"text", "text_delta"}:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _reasoning_delta(chunk: object) -> str:
    """提取 provider 显式 reasoning/thinking 增量，不解析普通正文。"""
    if not isinstance(chunk, AIMessageChunk):
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = chunk.additional_kwargs.get(key)
        if isinstance(value, str):
            return value
    if not isinstance(chunk.content, list):
        return ""
    parts: list[str] = []
    for block in chunk.content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") not in {
            "reasoning",
            "reasoning_content",
            "reasoning_delta",
            "thinking",
            "thinking_delta",
        }:
            continue
        for key in ("text", "reasoning_content", "reasoning", "thinking"):
            value = block.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    return "".join(parts)


def _stream_agent(metadata: object) -> object:
    if not isinstance(metadata, Mapping):
        return None
    return metadata.get("agent_role") or metadata.get("langgraph_node")


def _stream_events_from_item(
    item: object,
    *,
    session_id: str,
    run_id: str | None,
    sequence: int,
) -> tuple[int, list[StreamEvent]]:
    """Translate one LangGraph messages/custom item into ordered public events."""
    if not (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
    ):
        return sequence, []

    mode, data = item
    stream_events: list[StreamEvent] = []
    if mode == "messages" and isinstance(data, tuple) and len(data) == 2:
        chunk, metadata = data
        agent = _public_agent(_stream_agent(metadata))
        message_id = getattr(chunk, "id", None)
        public_message_id = message_id if isinstance(message_id, str) else None
        reasoning_delta = _reasoning_delta(chunk)
        if reasoning_delta:
            sequence += 1
            stream_events.append(
                StreamEvent(
                    event_type=StreamEventType.REASONING,
                    sequence=sequence,
                    session_id=session_id,
                    run_id=run_id,
                    agent=agent,
                    content=reasoning_delta,
                    message_id=public_message_id,
                    is_delta=True,
                )
            )
        delta = _text_delta(chunk)
        if delta:
            sequence += 1
            stream_events.append(
                StreamEvent(
                    event_type=StreamEventType.MESSAGE_DELTA,
                    sequence=sequence,
                    session_id=session_id,
                    run_id=run_id,
                    agent=agent,
                    content=delta,
                    message_id=public_message_id,
                    is_delta=True,
                )
            )
    elif mode == "custom" and isinstance(data, Mapping):
        raw_event = data.get("event") if data.get("kind") == "run_event" else None
        if isinstance(raw_event, Mapping):
            try:
                core_event = CoreRunEvent.model_validate(raw_event)
            except (TypeError, ValueError):
                core_event = None
            if core_event is not None:
                public_event = _public_event(core_event)
                if public_event is not None and public_event.event_type not in {
                    StreamEventType.MESSAGE_END,
                    StreamEventType.DONE,
                }:
                    sequence += 1
                    stream_events.append(
                        _stream_event_from_run_event(
                            public_event,
                            session_id,
                        ).model_copy(update={"sequence": sequence})
                    )
    return sequence, stream_events


async def _native_stream_events(
    graph: CollaborativeAgentGraph,
    payload: ChatRequest,
    request: Request,
    user_id: str | None,
    previous_state: AgentState | None,
    from_sequence: int,
    session: SessionRecord,
) -> AsyncIterator[str]:
    """把 LangGraph messages/custom 原生流桥接为 SSE，不轮询 checkpoint。"""
    run_id = str(uuid4())
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    # P2-7：附件提取文本拼入用户消息（无附件时与原消息逐字节一致）。
    # 提取含磁盘 IO / PDF 解析 / OCR 推理，走线程池避免阻塞事件循环
    #（审查 C1，与 pump 的 run_in_threadpool 同一模式）。
    message_text = await run_in_threadpool(
        compose_message_with_attachments,
        payload.message,
        payload.attachments,
        user_id,
        cast(OcrProvider | None, _ocr_provider_from_request(request)),
    )

    def pump() -> None:
        try:
            stream_method = graph.stream
            stream_kwargs = _workspace_call_kwargs(stream_method, session)
            if "run_id" in inspect.signature(stream_method).parameters:
                stream_kwargs["run_id"] = run_id
            stream_items = stream_method(
                message_text,
                payload.session_id,
                user_id,
                **stream_kwargs,
            )
            for item in stream_items:
                loop.call_soon_threadsafe(queue.put_nowait, ("item", item))
        except Exception as error:  # noqa: BLE001 - 稳定错误映射在异步边界完成
            loop.call_soon_threadsafe(queue.put_nowait, ("error", error))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

    pump_task = asyncio.create_task(run_in_threadpool(pump))
    sequence = max(_previous_sequence(previous_state), from_sequence)
    previous_count = _previous_message_count(previous_state)
    last_send_at = time.monotonic()
    error_emitted = False
    try:
        while True:
            try:
                item_type, item = await asyncio.wait_for(
                    queue.get(),
                    timeout=_POLL_INTERVAL_SECONDS,
                )
            except TimeoutError:
                if await request.is_disconnected():
                    await _wait_for_run(pump_task)
                    return
                if (
                    time.monotonic() - last_send_at
                    > _KEEPALIVE_INTERVAL_SECONDS
                ):
                    yield ": keepalive\n\n"
                    last_send_at = time.monotonic()
                continue

            if item_type == "error":
                sequence += 1
                yield _sse_frame(
                    await _error_event_for(
                        graph,
                        payload.session_id,
                        sequence,
                        cast(Exception, item),
                        user_id,
                        run_id,
                    )
                )
                return
            if item_type == "end":
                break
            sequence, stream_events = _stream_events_from_item(
                item,
                session_id=payload.session_id,
                run_id=run_id,
                sequence=sequence,
            )
            for stream_event in stream_events:
                yield _sse_frame(stream_event)
                if stream_event.event_type is StreamEventType.ERROR:
                    error_emitted = True
                last_send_at = time.monotonic()

        await pump_task
        state = await run_in_threadpool(
            graph.get_state,
            payload.session_id,
            user_id,
        )
        if state is None:
            sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.ERROR,
                    sequence=sequence,
                    session_id=payload.session_id,
                    run_id=run_id,
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                )
            )
            return
        state_error = _state_error_event(
            state,
            sequence + 1,
            payload.session_id,
        )
        pending_tool_approval = await pending_tool_approval_for_session(
            graph,
            payload.session_id,
            user_id,
        )
        if state_error is not None:
            if not error_emitted:
                sequence += 1
                yield _sse_frame(state_error)
        elif pending_tool_approval is not None and not error_emitted:
            sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.APPROVAL_REQUIRED,
                    sequence=sequence,
                    session_id=payload.session_id,
                    run_id=state.get("run_id") or run_id,
                    agent=pending_tool_approval.request.agent_role,
                    tool_name=pending_tool_approval.request.tool_name,
                    tool_call_id=pending_tool_approval.request.tool_call_id,
                    pending_tool_approval=pending_tool_approval,
                )
            )
        elif not error_emitted:
            final_message = _final_assistant_message(state, previous_count, user_id)
            if final_message is None:
                sequence += 1
                yield _sse_frame(
                    StreamEvent(
                        event_type=StreamEventType.ERROR,
                        sequence=sequence,
                        session_id=payload.session_id,
                        run_id=run_id,
                        agent=_public_agent(state.get("current_agent")),
                        error_code=ApiErrorCode.INTERNAL_ERROR,
                    )
                )
            else:
                sequence += 1
                yield _sse_frame(
                    StreamEvent(
                        event_type=StreamEventType.MESSAGE_END,
                        sequence=sequence,
                        session_id=payload.session_id,
                        run_id=run_id,
                        agent=_public_agent(state.get("current_agent")),
                        content=final_message.content,
                        message=final_message,
                        citations=_response_references(state, previous_count),
                        # P2-12：本轮批改结论与 citations 同位透出。
                        grading=_public_grading(state.get("grading")),
                    )
                )
        sequence += 1
        yield _sse_frame(
            StreamEvent(
                event_type=StreamEventType.DONE,
                sequence=sequence,
                session_id=payload.session_id,
                run_id=run_id,
            )
        )
    finally:
        if not pump_task.done():
            await _wait_for_run(pump_task)


async def tool_approval_stream_events(
    graph: CollaborativeAgentGraph,
    session_id: str,
    decision: ToolApprovalDecision,
    request: Request,
    user_id: str | None,
    previous_state: AgentState | None,
) -> AsyncIterator[str]:
    """Resume a tool gate and bridge terminal/model deltas to public SSE."""
    run_id = None if previous_state is None else previous_state.get("run_id")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def pump() -> None:
        try:
            for item in graph.stream_tool_approval(
                session_id,
                decision,
                user_id,
            ):
                loop.call_soon_threadsafe(queue.put_nowait, ("item", item))
        except Exception as error:  # noqa: BLE001 - mapped at the API boundary
            loop.call_soon_threadsafe(queue.put_nowait, ("error", error))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, ("end", None))

    pump_task = asyncio.create_task(run_in_threadpool(pump))
    sequence = _previous_sequence(previous_state)
    previous_count = _previous_message_count(previous_state)
    last_send_at = time.monotonic()
    error_emitted = False
    try:
        while True:
            try:
                item_type, item = await asyncio.wait_for(
                    queue.get(),
                    timeout=_POLL_INTERVAL_SECONDS,
                )
            except TimeoutError:
                if await request.is_disconnected():
                    await _wait_for_run(pump_task)
                    return
                if time.monotonic() - last_send_at > _KEEPALIVE_INTERVAL_SECONDS:
                    yield ": keepalive\n\n"
                    last_send_at = time.monotonic()
                continue

            if item_type == "error":
                sequence += 1
                yield _sse_frame(
                    await _error_event_for(
                        graph,
                        session_id,
                        sequence,
                        cast(Exception, item),
                        user_id,
                        run_id,
                    )
                )
                return
            if item_type == "end":
                break

            sequence, stream_events = _stream_events_from_item(
                item,
                session_id=session_id,
                run_id=run_id,
                sequence=sequence,
            )
            for stream_event in stream_events:
                yield _sse_frame(stream_event)
                if stream_event.event_type is StreamEventType.ERROR:
                    error_emitted = True
                last_send_at = time.monotonic()

        await pump_task
        state = await run_in_threadpool(graph.get_state, session_id, user_id)
        if state is None:
            sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.ERROR,
                    sequence=sequence,
                    session_id=session_id,
                    run_id=run_id,
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                )
            )
            return

        state_error = _state_error_event(state, sequence + 1, session_id)
        pending_tool_approval = await pending_tool_approval_for_session(
            graph,
            session_id,
            user_id,
        )
        if state_error is not None:
            if not error_emitted:
                sequence += 1
                yield _sse_frame(state_error)
        elif pending_tool_approval is not None and not error_emitted:
            sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.APPROVAL_REQUIRED,
                    sequence=sequence,
                    session_id=session_id,
                    run_id=state.get("run_id") or run_id,
                    agent=pending_tool_approval.request.agent_role,
                    tool_name=pending_tool_approval.request.tool_name,
                    tool_call_id=pending_tool_approval.request.tool_call_id,
                    pending_tool_approval=pending_tool_approval,
                )
            )
        elif not error_emitted:
            final_message = _final_assistant_message(state, previous_count, user_id)
            sequence += 1
            if final_message is None:
                yield _sse_frame(
                    StreamEvent(
                        event_type=StreamEventType.ERROR,
                        sequence=sequence,
                        session_id=session_id,
                        run_id=state.get("run_id") or run_id,
                        agent=_public_agent(state.get("current_agent")),
                        error_code=ApiErrorCode.INTERNAL_ERROR,
                    )
                )
            else:
                yield _sse_frame(
                    StreamEvent(
                        event_type=StreamEventType.MESSAGE_END,
                        sequence=sequence,
                        session_id=session_id,
                        run_id=state.get("run_id") or run_id,
                        agent=_public_agent(state.get("current_agent")),
                        content=final_message.content,
                        message=final_message,
                        citations=_response_references(state, previous_count),
                    )
                )

        sequence += 1
        yield _sse_frame(
            StreamEvent(
                event_type=StreamEventType.DONE,
                sequence=sequence,
                session_id=session_id,
                run_id=state.get("run_id") or run_id,
            )
        )
    finally:
        if not pump_task.done():
            await _wait_for_run(pump_task)


async def _stream_events(
    graph: CollaborativeAgentGraph,
    session_store: SessionStore,
    payload: ChatRequest,
    request: Request,
    user_id: str | None,
    active_session_lock: asyncio.Lock,
    from_sequence: int = 0,
) -> AsyncIterator[str]:
    """SSE 生成器：锁内转发原生流，兼容图则轮询，最后推权威终态。

    会话锁在生成器内持有(而不是路由函数内):锁必须覆盖整个流式
    推送期间,才能让并发的第二次请求在 locked() 检查时命中 session_busy。

    from_sequence(D1-T3):客户端断线重连时传「上次收到的最新 sequence」。
    若 checkpoint 中最近一轮已结束,直接回放剩余事件并补权威全文
    + done；正数游标遇到中间态时不启动 run，让客户端稍后再次续传。
    默认 0 才表示发新消息并启动新 run。
    """
    async with active_session_lock:
        session = await run_in_threadpool(
            _ensure_session_with_title,
            session_store,
            payload.session_id,
            user_id,
            payload.message,
            from_sequence == 0,
        )
        # 只读一次 checkpoint:回放检查与正常路径的 previous_state 共用
        # 同一份状态,避免重复取(两者本就要求同一时刻的快照)。
        current_state = await run_in_threadpool(
            graph.get_state, payload.session_id, user_id
        )
        events: list[CoreRunEvent] = (
            [] if current_state is None else current_state.get("events", [])
        )
        last_event = events[-1] if events else None
        pending_tool_approval = await pending_tool_approval_for_session(
            graph,
            payload.session_id,
            user_id,
        )
        if from_sequence > 0 and pending_tool_approval is not None:
            replay_sequence = max(
                from_sequence,
                _previous_sequence(current_state),
            )
            replay_sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.APPROVAL_REQUIRED,
                    sequence=replay_sequence,
                    session_id=payload.session_id,
                    run_id=(
                        None if current_state is None else current_state.get("run_id")
                    ),
                    agent=pending_tool_approval.request.agent_role,
                    tool_name=pending_tool_approval.request.tool_name,
                    tool_call_id=pending_tool_approval.request.tool_call_id,
                    pending_tool_approval=pending_tool_approval,
                )
            )
            replay_sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.DONE,
                    sequence=replay_sequence,
                    session_id=payload.session_id,
                    run_id=(
                        None if current_state is None else current_state.get("run_id")
                    ),
                )
            )
            return
        if (
            from_sequence > 0
            and current_state is not None
            and last_event is not None
            and last_event.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)
        ):
            # D1-T3 回放:token 帧使用 SSE 公开序列、不会写入 core
            # checkpoint,因此 from_sequence 可能远大于 core 终态序列。
            # 只要调用方明确传了重连游标且最近一轮已经终止,就绝不能
            # 重跑代理；先回放仍可见的运行事件,再补发权威全文。
            error_replayed = False
            for event in _public_events(events, from_sequence):
                if event.event_type in (
                    StreamEventType.MESSAGE_END,
                    StreamEventType.DONE,
                ):
                    # 与正常流一致:终态不推映射版，权威 message_end
                    # 与 done 由下方收尾统一合成。
                    continue
                stream_event = _stream_event_from_run_event(event, payload.session_id)
                yield _sse_frame(stream_event)
                if stream_event.event_type is StreamEventType.ERROR:
                    error_replayed = True
            replay_sequence = max(from_sequence, last_event.sequence)
            state_error = _state_error_event(
                current_state,
                replay_sequence + 1,
                payload.session_id,
            )
            if state_error is not None:
                if not error_replayed:
                    replay_sequence += 1
                    yield _sse_frame(state_error)
            elif not error_replayed:
                final_message = _final_assistant_message(current_state, 0, user_id)
                replay_sequence += 1
                if final_message is None:
                    yield _sse_frame(
                        StreamEvent(
                            event_type=StreamEventType.ERROR,
                            sequence=replay_sequence,
                            session_id=payload.session_id,
                            run_id=current_state.get("run_id"),
                            agent=_public_agent(current_state.get("current_agent")),
                            error_code=ApiErrorCode.INTERNAL_ERROR,
                        )
                    )
                else:
                    yield _sse_frame(
                        StreamEvent(
                            event_type=StreamEventType.MESSAGE_END,
                            sequence=replay_sequence,
                            session_id=payload.session_id,
                            run_id=current_state.get("run_id"),
                            agent=_public_agent(current_state.get("current_agent")),
                            content=final_message.content,
                            message=final_message,
                            citations=_response_references(current_state, 0),
                        )
                    )
            replay_sequence += 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.DONE,
                    sequence=replay_sequence,
                    session_id=payload.session_id,
                    run_id=current_state.get("run_id"),
                )
            )
            return

        if from_sequence > 0:
            # fail closed：带游标的请求一定是断线续传。真实长任务可能因
            # 客户端断开而仍在线程中运行，此时 checkpoint 只落了中间
            # 事件；若继续走下方 graph.stream/run，会把同一用户任务再
            # 执行一次。发一条 SSE 注释后结束（无 done），客户端会按
            # 原游标退避续传；即使服务已重启留下孤儿中间态，也宁可显式
            # 恢复失败，不能冒险重复有副作用的工具调用。
            yield ": reconnect-pending\n\n"
            return

        previous_state = current_state
        # 真实 CollaborativeAgentGraph 暴露 LangGraph 原生 messages/custom
        # 流：正文 token 与运行事件直接到达，不再等 checkpoint 轮询。
        # 旧测试替身/兼容实现没有 stream() 时仍走下方事件级轮询路径。
        native_stream = getattr(graph, "stream", None)
        if callable(native_stream):
            async for frame in _native_stream_events(
                graph,
                payload,
                request,
                user_id,
                previous_state,
                from_sequence,
                session,
            ):
                yield frame
            return

        last_sequence = _previous_sequence(previous_state)
        previous_count = _previous_message_count(previous_state)
        # P2-7：兼容路径同样消费附件；提取含磁盘 IO / PDF 解析 / OCR
        # 推理，先在线程池内完成组装（审查 C1），再后台启动图轮。
        message_text = await run_in_threadpool(
            compose_message_with_attachments,
            payload.message,
            payload.attachments,
            user_id,
            cast(OcrProvider | None, _ocr_provider_from_request(request)),
        )
        background_task = asyncio.create_task(
            run_in_threadpool(
                _run_graph_turn,
                graph,
                message_text,
                payload.session_id,
                user_id,
                session,
            )
        )
        last_send_at = time.monotonic()
        error_emitted = False
        try:
            while True:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                # 客户端断开:线程无法取消,等待 run 自然完成再退出
                # (锁保持到 run 结束,见 _wait_for_run 注释)。
                if await request.is_disconnected():
                    await _wait_for_run(background_task)
                    return

                # 轮询 checkpoint 拿「本轮新增事件」的增量,逐个推送。
                current_state = await run_in_threadpool(
                    graph.get_state, payload.session_id, user_id
                )
                events = (
                    [] if current_state is None else current_state.get("events", [])
                )
                for event in _public_events(events, last_sequence):
                    if event.event_type in (
                        StreamEventType.MESSAGE_END,
                        StreamEventType.DONE,
                    ):
                        # 终态不推映射版:message_end 的全文与 done 由
                        # 收尾统一合成,避免「无内容 message_end + 有
                        # 内容 message_end」并存与重复 done 的歧义
                        # (协议约定:带 content 的 message_end 才是终态)。
                        continue
                    last_sequence = event.sequence
                    stream_event = _stream_event_from_run_event(
                        event,
                        payload.session_id,
                    )
                    yield _sse_frame(stream_event)
                    if stream_event.event_type is StreamEventType.ERROR:
                        error_emitted = True
                    last_send_at = time.monotonic()

                # 事件稀疏的长 run:定期发 SSE 注释帧,防止中间代理
                # 因空闲断开连接(心跳不携带任何数据)。
                if (
                    time.monotonic() - last_send_at
                    > _KEEPALIVE_INTERVAL_SECONDS
                ):
                    yield ": keepalive\n\n"
                    last_send_at = time.monotonic()

                # run 已完成:取结果,推送 message_end(全文)+ done 收尾。
                if background_task.done():
                    if background_task.cancelled():
                        return
                    try:
                        state = background_task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:  # noqa: BLE001 - 图边界只暴露稳定错误数据
                        last_sequence += 1
                        yield _sse_frame(
                            await _error_event_for(
                                graph,
                                payload.session_id,
                                last_sequence,
                                error,
                                user_id,
                                None,
                            )
                        )
                        return

                    state_error = _state_error_event(
                        state,
                        last_sequence + 1,
                        payload.session_id,
                    )
                    pending_tool_approval = await pending_tool_approval_for_session(
                        graph,
                        payload.session_id,
                        user_id,
                    )
                    if state_error is not None:
                        if not error_emitted:
                            last_sequence += 1
                            yield _sse_frame(state_error)
                    elif pending_tool_approval is not None and not error_emitted:
                        last_sequence += 1
                        yield _sse_frame(
                            StreamEvent(
                                event_type=StreamEventType.APPROVAL_REQUIRED,
                                sequence=last_sequence,
                                session_id=payload.session_id,
                                run_id=state.get("run_id"),
                                agent=pending_tool_approval.request.agent_role,
                                tool_name=pending_tool_approval.request.tool_name,
                                tool_call_id=(
                                    pending_tool_approval.request.tool_call_id
                                ),
                                pending_tool_approval=pending_tool_approval,
                            )
                        )
                    elif not error_emitted:
                        final_message = _final_assistant_message(
                            state, previous_count, user_id
                        )
                        last_sequence += 1
                        if final_message is None:
                            yield _sse_frame(
                                StreamEvent(
                                    event_type=StreamEventType.ERROR,
                                    sequence=last_sequence,
                                    session_id=payload.session_id,
                                    run_id=state.get("run_id"),
                                    agent=_public_agent(state.get("current_agent")),
                                    error_code=ApiErrorCode.INTERNAL_ERROR,
                                )
                            )
                        else:
                            yield _sse_frame(
                                StreamEvent(
                                    event_type=StreamEventType.MESSAGE_END,
                                    sequence=last_sequence,
                                    session_id=payload.session_id,
                                    run_id=state.get("run_id"),
                                    agent=_public_agent(state.get("current_agent")),
                                    content=final_message.content,
                                    message=final_message,
                                    # D3-T5:message_end 携带本轮引用(与
                                    # POST /chat 的 references 同源,复用
                                    # _response_references 两级口径)——流式
                                    # 主通道的引用渲染依赖它(前端在 message_end
                                    # 事件读取 citations 存入 store)。
                                    citations=_response_references(
                                        state,
                                        previous_count,
                                    ),
                                )
                            )
                    last_sequence += 1
                    yield _sse_frame(
                        StreamEvent(
                            event_type=StreamEventType.DONE,
                            sequence=last_sequence,
                            session_id=payload.session_id,
                            run_id=state.get("run_id"),
                        )
                    )
                    return
        finally:
            # 任何提前退出路径(外部取消等)都确保后台任务不泄漏。
            # 注意:async generator 被 aclose 时 finally 中 await 会抛
            # RuntimeError(GeneratorExit 语义),用 suppress 兜住——
            # cancel 已标记任务,线程本身会自然结束。
            if not background_task.done():
                await _cancel_background_task(background_task)


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user_id)],
    from_sequence: int = Query(default=0, ge=0),
) -> Response:
    """SSE 原生流式聊天(token + 安全运行事件)。

    会话忙时与 POST /chat 行为一致:立即返回普通 JSON(session_busy),
    不是 SSE 流;正常时返回 text/event-stream,事件按 sequence 增量推送。

    from_sequence(D1-T3 断线重连):客户端断线重连时传上次收到的最新
    sequence；若 checkpoint 中最近一轮已结束,服务端回放剩余事件并补发
    权威 message_end + done；若仍是中间态则要求客户端继续等待，绝不
    重跑。默认 0 表示发新消息并启动新 run。
    """
    graph = _graph(request)
    session_store = _session_store(request)
    active_session_lock = session_lock(request, payload.session_id, user_id)
    if active_session_lock.locked():
        return JSONResponse(
            content=session_busy_response(
                payload.session_id,
                "Another request is already running for this session.",
            ).model_dump(mode="json"),
            status_code=200,
        )

    return StreamingResponse(
        _stream_events(
            graph,
            session_store,
            payload,
            request,
            user_id,
            active_session_lock,
            from_sequence,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
