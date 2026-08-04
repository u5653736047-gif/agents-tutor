"""SSE 事件级流式聊天端点(D1-T1,D1-T3 断线重连与消息补发)。

与 POST /chat 的差异:POST /chat 在一个请求内等待完整 run 结束并返回
最终契约;本端点把 run 放到后台线程,事件按 sequence 增量通过 SSE
帧逐条推送,最后以 message_end(携带最终消息全文)+ done 收尾。

为什么是「事件级」而不是 token 级:core 是同步 ReAct 编排
(CollaborativeAgentGraph.run 一次性跑完一轮),没有 token 流可暴露;
可流式化的粒度是运行事件(RunEvent),由 checkpoint 的 get_state 轮询
增量读取。thinking 事件只带固定占位文本,message_end 一次性携带全文。

D1-T3 断线重连与消息补发:客户端可在查询参数传 from_sequence
(上次收到的最新 sequence)。若 checkpoint 中已存在比 from_sequence
更新的运行事件且最近一轮已结束(最后事件是终态),本端点直接回放
剩余事件 + done,不启动新 run(避免断线重试导致整轮重复执行);
否则按正常路径启动新 run。sequence 跨轮次全局递增(core 侧 events
通道跨轮累积),回放只对 from_sequence > 0 生效——正常发新消息时
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
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from api.chat import (
    PENDING_RESUME_ERROR_PREFIX,
    _ensure_session,
    _final_assistant_message,
    _previous_message_count,
    _previous_sequence,
    _public_agent,
    _public_events,
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


async def _wait_for_run(background_task: asyncio.Task[AgentState]) -> None:
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


async def _stream_events(
    graph: CollaborativeAgentGraph,
    session_store: SessionStore,
    payload: ChatRequest,
    request: Request,
    user_id: str | None,
    active_session_lock: asyncio.Lock,
    from_sequence: int = 0,
) -> AsyncIterator[str]:
    """SSE 生成器:锁内启动后台 run,轮询增量事件,收尾推送 message_end + done。

    会话锁在生成器内持有(而不是路由函数内):锁必须覆盖整个流式
    推送期间,才能让并发的第二次请求在 locked() 检查时命中 session_busy。

    from_sequence(D1-T3):客户端断线重连时传「上次收到的最新 sequence」。
    若 checkpoint 已存在比 from_sequence 更新的终态轮次,直接回放剩余
    事件 + done,不启动新 run;否则正常启动新 run(默认 0 表示发新消息,
    必须启动新 run——回放仅对 from_sequence > 0 生效,见模块 docstring)。
    """
    async with active_session_lock:
        await run_in_threadpool(
            _ensure_session, session_store, payload.session_id, user_id
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
            and last_event is not None
            and last_event.event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED)
            and last_event.sequence > from_sequence
        ):
            # D1-T3 回放:上次连接中断后重连——最近一轮已结束(最后事件
            # 是终态)且存在比 from_sequence 更新的运行事件,直接回放
            # 剩余事件 + done,不启动新 run(避免重复执行)。回放路径
            # 不合成 message_end 全文:前端在 done 后调用历史接口拉
            # 权威消息(D1-T3 验收「重连后回放剩余事件并以 done 收尾」)。
            for event in _public_events(events, from_sequence):
                if event.event_type in (
                    StreamEventType.MESSAGE_END,
                    StreamEventType.DONE,
                ):
                    # 与正常流一致:终态不推映射版(message_end 的全文
                    # 与 done 由收尾统一合成,回放路径只合成 done)。
                    continue
                yield _sse_frame(
                    _stream_event_from_run_event(event, payload.session_id)
                )
            # done 的 sequence 必须大于「真实终态序列」:公开事件序列
            # 可能有间隙(TASK_RESULTS_AGGREGATED 等被 EVENT_TYPE_MAP
            # 过滤),用 events[-1].sequence + 1 而非 last_sequence + 1
            # (review 修正)——保证调用方把 done.sequence 传回时,回放
            # 条件「终态 > from_sequence」必然不成立,下一条消息正常
            # 启动新 run,不会被误判为回放而静默吞掉。
            done_sequence = last_event.sequence + 1
            yield _sse_frame(
                StreamEvent(
                    event_type=StreamEventType.DONE,
                    sequence=done_sequence,
                    session_id=payload.session_id,
                )
            )
            return

        previous_state = current_state
        last_sequence = _previous_sequence(previous_state)
        previous_count = _previous_message_count(previous_state)
        background_task = asyncio.create_task(
            run_in_threadpool(graph.run, payload.message, payload.session_id, user_id)
        )
        last_send_at = time.monotonic()
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
                    yield _sse_frame(
                        _stream_event_from_run_event(event, payload.session_id)
                    )
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

                    final_message = _final_assistant_message(state, previous_count)
                    if final_message is not None:
                        last_sequence += 1
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
                                citations=_response_references(state, previous_count),
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
    """SSE 事件级流式聊天(非 token 级,core 同步 ReAct)。

    会话忙时与 POST /chat 行为一致:立即返回普通 JSON(session_busy),
    不是 SSE 流;正常时返回 text/event-stream,事件按 sequence 增量推送。

    from_sequence(D1-T3 断线重连):客户端断线重连时传上次收到的最新
    sequence;若 checkpoint 中最近一轮已结束且存在更新的运行事件,服务端
    回放剩余事件 + done 收尾,不启动新 run(消息补发);默认 0 表示发新
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
