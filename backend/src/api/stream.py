"""SSE 原生流式聊天端点(D1-T1,D1-T3 断线重连与消息补发)。

与 POST /chat 的差异:POST /chat 在一个请求内等待完整 run 结束并返回
最终契约;本端点消费 LangGraph 的 messages/custom 双通道,正文 token
与安全运行事件按 sequence 增量通过 SSE 推送,最后以 message_end
(携带权威全文)+ done 收尾。没有原生 stream() 的兼容图仍使用 checkpoint
轮询事件,但生产图不走该降级路径。

D1-T3 断线重连与消息补发:客户端可在查询参数传 from_sequence
(上次收到的最新公开 sequence)。若 checkpoint 中最近一轮已结束
(最后事件是终态),本端点直接回放剩余事件并补发
权威 message_end + done,不启动新 run(避免断线重试导致整轮重复执行);
否则按正常路径启动新 run。core 事件序列随 checkpoint 跨轮累积，
token 帧则只属于当前公开流；回放只对 from_sequence > 0 生效——正常发新消息时
默认 from_sequence=0 必须启动新 run,否则会把上一轮误判为重连回放。

事件安全红线(与 api/chat.py 同口径):
- tool_call / tool_result 事件只含工具名、成功与否、耗时等摘要,
  绝不含工具参数与结果正文(_public_event 的字段白名单保证);
- thinking 事件的 content 是固定占位文本(如 Agent 名),绝不伪造
  模型中间输出;
- message_end 的 content 是最终消息全文(与 POST /chat 的
  ChatResponse.message.content 同源)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import AIMessageChunk
from starlette.concurrency import run_in_threadpool

from api.chat import (
    PENDING_RESUME_ERROR_PREFIX,
    _ensure_session_with_title,
    _final_assistant_message,
    _previous_message_count,
    _previous_sequence,
    _public_agent,
    _public_event,
    _public_events,
    _public_run_error,
    _response_references,
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
from core.sessions import SessionStore
from core.state import AgentState

router = APIRouter(tags=["chat"])
_LOGGER = logging.getLogger("api.stream")

# SSE 轮询间隔:core 是同步 run,事件在 checkpoint 里逐步落盘,
# 生成器以固定间隔轮询 get_state 拿增量(50ms 对事件级推送足够)。
_POLL_INTERVAL_SECONDS = 0.05
# SSE 心跳间隔:事件稀疏的长 run 期间,定期发注释帧
# (": keepalive\n\n")防止中间代理因空闲断开;心跳不携带任何数据。
_KEEPALIVE_INTERVAL_SECONDS = 15.0


def _graph(request: Request) -> CollaborativeAgentGraph:
    return cast(CollaborativeAgentGraph, request.app.state.graph)


def _session_store(request: Request) -> SessionStore:
    return cast(SessionStore, request.app.state.session_store)


def _stream_event_from_run_event(event: RunEvent, session_id: str) -> StreamEvent:
    """把公开 RunEvent 转成流式 StreamEvent(不新增任何工具细节)。

    thinking 事件补固定占位文本(如「supervisor 开始处理」),绝不使用
    模型中间输出;其余字段与 RunEvent 一一对应,content 保持 None。
    """
    if event.event_type is StreamEventType.THINKING:
        # AgentRole 是 str 枚举,f-string 直接格式化会输出
        # "AgentRole.SUPERVISOR",取 .value 得到 "supervisor"。
        agent_name = event.agent.value if event.agent is not None else None
        content = (
            f"{agent_name} 开始处理" if agent_name is not None else "该 Agent 开始处理"
        )
    else:
        content = None
    return StreamEvent(
        event_type=event.event_type,
        sequence=event.sequence,
        session_id=event.session_id or session_id,
        agent=event.agent,
        tool_name=event.tool_name,
        success=event.success,
        duration_ms=event.duration_ms,
        error_code=event.error_code,
        plan_step_sequence=event.plan_step_sequence,
        content=content,
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
        agent=run_error.agent,
        error_code=run_error.error_code,
    )


async def _error_event_for(
    graph: CollaborativeAgentGraph,
    session_id: str,
    sequence: int,
    error: Exception,
    user_id: str | None,
) -> StreamEvent:
    """把后台 run 的异常映射为脱敏 error 事件(与 POST /chat 分类一致)。

    - RuntimeError 且会话存在待恢复 handoff(或消息以
      PENDING_RESUME_ERROR_PREFIX 开头)→ session_busy;
    - 其余异常 → internal_error。异常正文绝不进入事件(脱敏)。
    """
    if isinstance(error, RuntimeError):
        pending = await run_in_threadpool(graph.get_pending_handoff, session_id, user_id)
        if pending is not None or str(error).startswith(PENDING_RESUME_ERROR_PREFIX):
            return StreamEvent(
                event_type=StreamEventType.ERROR,
                sequence=sequence,
                session_id=session_id,
                error_code=ApiErrorCode.SESSION_BUSY,
            )
    return StreamEvent(
        event_type=StreamEventType.ERROR,
        sequence=sequence,
        session_id=session_id,
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
    """只提取公开文本块；reasoning/tool-call 块与附加字段一律忽略。"""
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


def _stream_agent(metadata: object) -> object:
    if not isinstance(metadata, Mapping):
        return None
    return metadata.get("agent_role") or metadata.get("langgraph_node")


async def _native_stream_events(
    graph: CollaborativeAgentGraph,
    payload: ChatRequest,
    request: Request,
    user_id: str | None,
    previous_state: AgentState | None,
    from_sequence: int,
) -> AsyncIterator[str]:
    """把 LangGraph messages/custom 原生流桥接为 SSE，不轮询 checkpoint。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def pump() -> None:
        try:
            for item in graph.stream(
                payload.message,
                payload.session_id,
                user_id,
            ):
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
                    )
                )
                return
            if item_type == "end":
                break
            if not (
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
            ):
                continue
            mode, data = item
            stream_event: StreamEvent | None = None
            if mode == "messages" and isinstance(data, tuple) and len(data) == 2:
                chunk, metadata = data
                delta = _text_delta(chunk)
                if delta:
                    sequence += 1
                    message_id = getattr(chunk, "id", None)
                    stream_event = StreamEvent(
                        event_type=StreamEventType.MESSAGE_DELTA,
                        sequence=sequence,
                        session_id=payload.session_id,
                        agent=_public_agent(_stream_agent(metadata)),
                        content=delta,
                        message_id=(message_id if isinstance(message_id, str) else None),
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
                            stream_event = _stream_event_from_run_event(
                                public_event,
                                payload.session_id,
                            ).model_copy(update={"sequence": sequence})
            if stream_event is not None:
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
                    error_code=ApiErrorCode.INTERNAL_ERROR,
                )
            )
            return
        state_error = _state_error_event(
            state,
            sequence + 1,
            payload.session_id,
        )
        if state_error is not None:
            if not error_emitted:
                sequence += 1
                yield _sse_frame(state_error)
        elif not error_emitted:
            final_message = _final_assistant_message(state, previous_count)
            if final_message is None:
                sequence += 1
                yield _sse_frame(
                    StreamEvent(
                        event_type=StreamEventType.ERROR,
                        sequence=sequence,
                        session_id=payload.session_id,
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
                session_id=payload.session_id,
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
    + done,不启动新 run;否则正常启动新 run(默认 0 表示发新消息,
    必须启动新 run——回放仅对 from_sequence > 0 生效,见模块 docstring)。
    """
    async with active_session_lock:
        await run_in_threadpool(
            _ensure_session_with_title,
            session_store,
            payload.session_id,
            user_id,
            payload.message,
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
                final_message = _final_assistant_message(current_state, 0)
                replay_sequence += 1
                if final_message is None:
                    yield _sse_frame(
                        StreamEvent(
                            event_type=StreamEventType.ERROR,
                            sequence=replay_sequence,
                            session_id=payload.session_id,
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
                )
            )
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
            ):
                yield frame
            return

        last_sequence = _previous_sequence(previous_state)
        previous_count = _previous_message_count(previous_state)
        background_task = asyncio.create_task(
            run_in_threadpool(graph.run, payload.message, payload.session_id, user_id)
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
                            )
                        )
                        return

                    state_error = _state_error_event(
                        state,
                        last_sequence + 1,
                        payload.session_id,
                    )
                    if state_error is not None:
                        if not error_emitted:
                            last_sequence += 1
                            yield _sse_frame(state_error)
                    elif not error_emitted:
                        final_message = _final_assistant_message(state, previous_count)
                        last_sequence += 1
                        if final_message is None:
                            yield _sse_frame(
                                StreamEvent(
                                    event_type=StreamEventType.ERROR,
                                    sequence=last_sequence,
                                    session_id=payload.session_id,
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
    sequence;若 checkpoint 中最近一轮已结束,服务端回放剩余事件并补发
    权威 message_end + done,不启动新 run(消息补发);默认 0 表示发新
    消息,启动新 run。
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
