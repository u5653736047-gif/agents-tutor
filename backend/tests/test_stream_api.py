"""SSE 流式聊天端点(D1-T1)测试。

仿照 test_chat_api.py 的替身模式:通过 `app.state.graph = 替身`
注入可控的同步图(与 test_chat_api 的 _chat_app 同一注入方式,
create_app() 不跑 lifespan,state 直接赋值即可),验证:

1. 事件按 sequence 增量依序到达,message_end 携带最终消息全文,
   done 收尾;
2. run 异常 → error 事件(internal_error / session_busy),HTTP 仍 200;
3. 会话忙 → 第二次请求立即返回 JSON session_busy(非 SSE 流);
4. 工具事件(tool_call/tool_result)不含工具参数/结果正文;
5. thinking 事件 content 为固定占位文本(非模型输出)。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from langchain_core.messages import AIMessage, HumanMessage

from api.app import create_app
from api.schemas import ChatRequest
from api.stream import _stream_events
from core.events import EventType, RunEvent
from core.sessions import SessionStore

# ── 图替身:与 test_chat_api.ChatGraph 同构,但 get_state 返回
#    「当前可见」状态(中间态 → 最终态),模拟 checkpoint 逐步落盘 ──


class StreamingChatGraph:
    """流式替身:run 在后台线程延时完成,get_state 返回可见状态快照。

    - initial_state:run 启动前 get_state 返回的状态(生成器开头取
      previous_state 用,应为空事件);
    - intermediate_state:run 启动后、完成前 get_state 返回的状态
      (模拟执行中的 checkpoint,含部分事件);
    - run 完成后 _visible_state 切换为 final_state(含全部事件),
      生成器靠轮询增量拿到剩余事件。
    """

    def __init__(
        self,
        final_state: dict[str, Any],
        *,
        initial_state: dict[str, Any] | None = None,
        intermediate_state: dict[str, Any] | None = None,
        run_delay: float = 0.3,
        run_exception: Exception | None = None,
    ) -> None:
        self._final_state = final_state
        self._initial_state = initial_state
        self._intermediate_state = intermediate_state
        self._run_delay = run_delay
        self._run_exception = run_exception
        self._visible_state = intermediate_state
        self.run_started = threading.Event()
        self.run_thread_id: int | None = None
        self.run_inputs: list[tuple[str, str, str | None]] = []

    def get_state(
        self, session_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        # run 启动前(生成器取 previous_state)返回初始态,保证
        # last_sequence 从 run 前的事件序列算起,不会跳过本轮增量。
        if not self.run_started.is_set():
            return self._initial_state
        return self._visible_state

    def run(
        self, user_input: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        self.run_started.set()
        self.run_thread_id = threading.get_ident()
        self.run_inputs.append((user_input, session_id, user_id))
        time.sleep(self._run_delay)
        if self._run_exception is not None:
            raise self._run_exception
        self._visible_state = self._final_state
        return self._final_state

    def get_pending_handoff(
        self, session_id: str, user_id: str | None = None
    ) -> object | None:
        return None


class BlockingStreamingChatGraph(StreamingChatGraph):
    """run 阻塞直到测试释放,用于并发/会话忙测试(仿 BlockingChatGraph)。"""

    def __init__(self) -> None:
        super().__init__(
            {
                "messages": [
                    HumanMessage(content="first"),
                    AIMessage(content="complete"),
                ],
                "events": [],
                "current_agent": "supervisor",
            },
            run_delay=0.0,
        )
        self.release_run = threading.Event()

    def run(
        self, user_input: str, session_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        self.run_started.set()
        if not self.release_run.wait(timeout=2):
            raise TimeoutError("test run was not released")
        return super().run(user_input, session_id, user_id)


def _chat_app(tmp_path: Path, graph: StreamingChatGraph) -> tuple[FastAPI, SessionStore]:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    app.state.graph = graph
    return app, store


async def _post_stream(app: FastAPI, body: dict[str, str]) -> list[dict[str, Any]]:
    """POST /chat/stream 并解析全部 SSE 帧(断言 HTTP 200 与流媒体类型)。"""
    transport = ASGITransport(app=app)
    frames: list[dict[str, Any]] = []
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        client.stream(
            "POST",
            "/chat/stream",
            headers={"X-User-Id": "user-1"},
            json=body,
        ) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            frames.append(json.loads(line[len("data: "):]))
    return frames


def test_stream_events_arrive_in_sequence_and_end_with_message_end_then_done(
    tmp_path: Path,
) -> None:
    """事件按 sequence 增量依序到达,最后是 message_end(全文)+ done。"""
    intermediate_events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=0,
            session_id="session-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=1,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
        ),
        RunEvent(
            event_type=EventType.TOOL_COMPLETED,
            sequence=2,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            success=True,
        ),
    ]
    final_events = [
        *intermediate_events,
        RunEvent(
            event_type=EventType.AGENT_COMPLETED,
            sequence=3,
            session_id="session-1",
            agent="supervisor",
            success=True,
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=4,
            session_id="session-1",
            success=True,
        ),
    ]
    graph = StreamingChatGraph(
        {
            "messages": [HumanMessage(content="请评估"), AIMessage(content="评估完成")],
            "events": final_events,
            "current_agent": "supervisor",
        },
        # 中间态与最终态同事件集:run 完成前(首轮 50ms 轮询,远早于
        # 0.3s 的 run_delay)全部事件即可见,避免「轮询读状态」与
        # 「run 完成」交错导致的帧数竞态;增量推送的「部分事件可见」
        # 场景由 test_stream_tool_events_never_carry_tool_arguments_or_results
        # 覆盖。
        intermediate_state={
            "events": final_events,
            "messages": [HumanMessage(content="请评估"), AIMessage(content="评估完成")],
        },
    )
    app, store = _chat_app(tmp_path, graph)
    caller_thread = threading.get_ident()
    try:
        frames = asyncio.run(
            _post_stream(app, {"session_id": "session-1", "message": "请评估"})
        )
    finally:
        store.close()

    # run 必须在线程池执行(核心同步调用不得阻塞事件循环)。
    assert graph.run_thread_id is not None
    assert graph.run_thread_id != caller_thread
    assert graph.run_inputs == [("请评估", "session-1", "user-1")]

    # 终态映射事件(AGENT_COMPLETED/RUN_COMPLETED)不推:message_end 的
    # 全文与 done 由生成器收尾统一合成,事件流里 message_end 总是带
    # 全文、done 只有一条(避免「无内容 message_end + 有内容
    # message_end」并存与重复 done 的歧义)。
    assert [frame["event_type"] for frame in frames] == [
        "thinking",
        "tool_call",
        "tool_result",
        "message_end",
        "done",
    ]
    assert [frame["sequence"] for frame in frames] == [0, 1, 2, 3, 4]
    assert frames[-1]["event_type"] == "done"

    # 合成的 message_end 携带最终消息全文(与 POST /chat 的 message 同源)。
    message_end_frames = [
        frame for frame in frames if frame["event_type"] == "message_end"
    ]
    assert message_end_frames[-1]["content"] == "评估完成"
    assert message_end_frames[-1]["message"]["content"] == "评估完成"

    # thinking 事件 content 是固定占位文本(非模型输出)。
    assert frames[0]["content"] == "supervisor 开始处理"
    assert frames[0]["agent"] == "supervisor"


def test_stream_reports_unexpected_run_errors_as_internal_error_events(
    tmp_path: Path,
) -> None:
    """run 抛非 pending RuntimeError → error 事件(internal_error),HTTP 200。"""
    graph = StreamingChatGraph(
        {},
        intermediate_state={"events": [], "messages": []},
        run_exception=RuntimeError("internal invariant failed"),
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(app, {"session_id": "session-1", "message": "go"})
        )
    finally:
        store.close()

    assert [frame["event_type"] for frame in frames] == ["error"]
    assert frames[0]["error_code"] == "internal_error"
    # 异常正文绝不进入事件(脱敏)。
    assert "internal invariant failed" not in json.dumps(frames)


def test_stream_maps_pending_resume_errors_to_session_busy(tmp_path: Path) -> None:
    """run 抛「存在待恢复执行」RuntimeError → error 事件(session_busy)。"""
    graph = StreamingChatGraph(
        {},
        intermediate_state={"events": [], "messages": []},
        run_exception=RuntimeError("存在待恢复执行，请先调用 resume_handoff()"),
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(app, {"session_id": "session-1", "message": "请继续"})
        )
    finally:
        store.close()

    assert [frame["event_type"] for frame in frames] == ["error"]
    assert frames[0]["error_code"] == "session_busy"


def test_stream_returns_session_busy_json_when_the_session_is_locked(
    tmp_path: Path,
) -> None:
    """会话忙:第二次请求立即返回普通 JSON session_busy(非 SSE 流)。"""
    graph = BlockingStreamingChatGraph()
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def consume_stream(client: AsyncClient) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        async with client.stream(
            "POST",
            "/chat/stream",
            headers={"X-User-Id": "user-1"},
            json={"session_id": "session-1", "message": "first"},
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                frames.append(json.loads(line[len("data: "):]))
        return frames

    async def send_concurrent_requests() -> tuple[list[dict[str, Any]], Response]:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_task = asyncio.create_task(consume_stream(client))
            await asyncio.wait_for(asyncio.to_thread(graph.run_started.wait), timeout=1)
            second_response = await client.post(
                "/chat/stream",
                headers={"X-User-Id": "user-1"},
                json={"session_id": "session-1", "message": "second"},
            )
            try:
                graph.release_run.set()
                first_frames = await first_task
            finally:
                graph.release_run.set()
                if not first_task.done():
                    await first_task
            return first_frames, second_response

    try:
        first_frames, second_response = asyncio.run(send_concurrent_requests())
    finally:
        store.close()

    # 第二次请求:锁被第一次流持有 → 与 POST /chat 同构的 JSON session_busy。
    assert second_response.status_code == 200
    assert second_response.headers["content-type"].startswith("application/json")
    assert second_response.json()["run_error"] == {
        "error_code": "session_busy",
        "message": "Another request is already running for this session.",
        "agent": None,
    }
    assert second_response.json()["events"] == []
    assert graph.run_inputs == [("first", "session-1", "user-1")]

    # 第一次流在锁释放后正常收尾(done 是最后一个事件)。
    assert first_frames[-1]["event_type"] == "done"


def test_stream_tool_events_never_carry_tool_arguments_or_results(
    tmp_path: Path,
) -> None:
    """tool_call/tool_result 事件只含摘要字段,绝不含参数/结果正文。

    更强形式:所有 SSE 帧的键集合必须落在 StreamEvent 的公开字段内,
    帧里出现 args/result/tool_input/output 等键即视为契约破坏。
    """
    events = [
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=0,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
        ),
        RunEvent(
            event_type=EventType.TOOL_COMPLETED,
            sequence=1,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            success=True,
        ),
        RunEvent(
            event_type=EventType.AGENT_COMPLETED,
            sequence=2,
            session_id="session-1",
            agent="supervisor",
            success=True,
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=3,
            session_id="session-1",
            success=True,
        ),
    ]
    graph = StreamingChatGraph(
        {
            "messages": [HumanMessage(content="请检索"), AIMessage(content="检索完成")],
            "events": events,
            "current_agent": "supervisor",
        },
        intermediate_state={
            "events": events[:2],
            "messages": [HumanMessage(content="请检索")],
        },
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(app, {"session_id": "session-1", "message": "请检索"})
        )
    finally:
        store.close()

    allowed_keys = {
        "event_type",
        "sequence",
        "session_id",
        "agent",
        "tool_name",
        "success",
        "duration_ms",
        "error_code",
        "plan_step_sequence",
        "content",
        "message",
        "citations",
        "current_agent",
    }
    for frame in frames:
        assert set(frame).issubset(allowed_keys)
    tool_frames = [
        frame for frame in frames if frame["event_type"] in {"tool_call", "tool_result"}
    ]
    assert len(tool_frames) == 2
    for frame in tool_frames:
        assert "args" not in frame
        assert "result" not in frame
        assert "tool_input" not in frame
        assert "output" not in frame
        assert frame["tool_name"] == "search_knowledge"


def test_stream_disconnect_waits_for_run_then_releases_lock(
    tmp_path: Path,
) -> None:
    """客户端中途断开:后台 run 仍自然完成,会话锁保持到 run 结束。

    review 修正:线程池里的同步 run 无法被 asyncio 取消,若断连后直接
    退出生成器,锁会提前释放——后续请求可能与旧 run 并发写同一会话。
    实现改为断连路径等待 run 完成再退出(_wait_for_run),本测试直接
    驱动 _stream_events 生成器锁定该语义:

    1. fake request 的 is_disconnected 恒为 True(首个轮询周期即断开);
    2. 生成器退出前(run 完成前)会话锁必须仍被持有;
    3. run 完成后生成器退出、锁释放——第二次请求可正常获取锁。

    为什么不用 httpx 中途 aclose 模拟:ASGITransport 在 app(生成器)
    完全结束后才返回 Response,客户端 aclose 时服务端早已收尾,
    is_disconnected 永远返回 False(伪断连),无法覆盖本路径。
    """
    events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=0,
            session_id="session-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=1,
            session_id="session-1",
            success=True,
        ),
    ]
    graph = StreamingChatGraph(
        {
            "messages": [HumanMessage(content="hi"), AIMessage(content="done")],
            "events": events,
            "current_agent": "supervisor",
        },
        intermediate_state={"events": [], "messages": []},
        run_delay=0.4,
    )
    store = SessionStore(tmp_path / "sessions.sqlite3")
    payload = ChatRequest(session_id="session-1", message="hi")

    class _DisconnectedRequest:
        """is_disconnected 恒 True:首个轮询周期即走断连路径。"""

        async def is_disconnected(self) -> bool:
            return True

    try:
        async def scenario() -> None:
            lock = asyncio.Lock()
            generator = _stream_events(
                graph,
                store,
                payload,
                _DisconnectedRequest(),  # type: ignore[arg-type]  # 测试替身只实现所需方法
                "user-1",
                lock,
            )

            async def drive() -> list[str]:
                return [frame async for frame in generator]

            drive_task = asyncio.create_task(drive())
            # 首个轮询周期(50ms)内进入断连路径:run 已启动、锁仍持有。
            await asyncio.sleep(0.1)
            assert graph.run_thread_id is not None
            assert lock.locked()
            # run(0.4s)完成后生成器退出,锁释放。
            await drive_task
            assert not lock.locked()
            # 锁释放后第二次请求可正常获取(等价于不再 session_busy)。
            assert await lock.acquire()  # 能获取即证明已释放
            lock.release()

        asyncio.run(scenario())
    finally:
        store.close()
