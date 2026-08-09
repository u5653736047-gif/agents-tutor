"""SSE 流式聊天端点(D1-T1)测试。

仿照 test_chat_api.py 的替身模式:通过 `app.state.graph = 替身`
注入可控的同步图(与 test_chat_api 的 _chat_app 同一注入方式,
create_app() 不跑 lifespan,state 直接赋值即可),验证:

1. 事件按 sequence 增量依序到达,message_end 携带最终消息全文,
   done 收尾;
2. run 异常 → error 事件(internal_error / session_busy),HTTP 仍 200;
3. 会话忙 → 第二次请求立即返回 JSON session_busy(非 SSE 流);
4. 工具事件(tool_call/tool_result)携带有界、脱敏的输入/输出摘要;
5. provider 的 reasoning 字段作为独立增量事件流式输出。
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
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from api.app import create_app
from api.schemas import ChatRequest
from api.stream import _stream_events
from core.events import ErrorCode, EventType, RunError, RunEvent
from core.sessions import SessionStore
from core.state import AgentRole, PendingToolApproval, ToolApprovalRequest

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


class NativeTokenStreamingChatGraph(StreamingChatGraph):
    """直接产出 LangGraph messages/custom 流，模拟真实 token 到达。"""

    def __init__(self) -> None:
        super().__init__(
            {
                "messages": [
                    HumanMessage(content="解释流式"),
                    AIMessage(content="流式完成"),
                ],
                "events": [
                    RunEvent(
                        event_type=EventType.AGENT_STARTED,
                        sequence=0,
                        session_id="session-1",
                        agent="supervisor",
                    ),
                    RunEvent(
                        event_type=EventType.AGENT_COMPLETED,
                        sequence=1,
                        session_id="session-1",
                        agent="supervisor",
                        success=True,
                    ),
                    RunEvent(
                        event_type=EventType.RUN_COMPLETED,
                        sequence=2,
                        session_id="session-1",
                        agent="supervisor",
                        success=True,
                    ),
                ],
                "current_agent": "supervisor",
            },
            initial_state={"events": [], "messages": []},
            run_delay=0.0,
        )
        self.release = threading.Event()
        self.finished = threading.Event()
        self.stream_thread_id: int | None = None

    def stream(
        self,
        user_input: str,
        session_id: str,
        user_id: str | None = None,
    ) -> object:
        self.run_started.set()
        self.stream_thread_id = threading.get_ident()
        self.run_inputs.append((user_input, session_id, user_id))
        started = RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=0,
            session_id=session_id,
            agent="supervisor",
        )
        yield (
            "custom",
            {"kind": "run_event", "event": started.model_dump(mode="json")},
        )
        # provider 显式返回的 reasoning_content 是独立过程通道，不混入正文。
        yield (
            "messages",
            (
                AIMessageChunk(
                    content="",
                    additional_kwargs={"reasoning_content": "先识别问题，再组织解释"},
                    id="assistant-turn",
                ),
                {"agent_role": "supervisor"},
            ),
        )
        yield (
            "messages",
            (
                AIMessageChunk(content="流", id="assistant-turn"),
                {"agent_role": "supervisor"},
            ),
        )
        self.release.wait(timeout=2)
        yield (
            "messages",
            (
                AIMessageChunk(content="式", id="assistant-turn"),
                {"agent_role": "supervisor"},
            ),
        )
        self._visible_state = self._final_state
        self.finished.set()


class NativeTerminalStateChatGraph(StreamingChatGraph):
    """原生流结束后只通过 checkpoint 暴露终态，模拟生产图收口。"""

    def __init__(self, final_state: dict[str, Any]) -> None:
        super().__init__(
            final_state,
            initial_state={"events": [], "messages": []},
            run_delay=0.0,
        )

    def stream(
        self,
        user_input: str,
        session_id: str,
        user_id: str | None = None,
    ) -> object:
        self.run_started.set()
        self.run_inputs.append((user_input, session_id, user_id))
        started = RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=0,
            session_id=session_id,
            agent="supervisor",
        )
        yield (
            "custom",
            {"kind": "run_event", "event": started.model_dump(mode="json")},
        )
        self._visible_state = self._final_state


class NativePendingToolChatGraph(NativeTerminalStateChatGraph):
    def __init__(self) -> None:
        super().__init__(
            {
                "run_id": "run-shell-1",
                "messages": [HumanMessage(content="inspect")],
                "events": [],
                "current_agent": "supervisor",
                "pending_tool_approval": {
                    "tool_call_id": "shell-1",
                    "tool_name": "shell",
                    "agent_role": "supervisor",
                    "arguments": {"command": "git status", "cwd": "."},
                },
            }
        )
        self.pending = PendingToolApproval(
            interrupt_id="interrupt-shell-1",
            request=ToolApprovalRequest(
                tool_call_id="shell-1",
                tool_name="shell",
                agent_role=AgentRole.SUPERVISOR,
                arguments={"command": "git status", "cwd": "."},
            ),
        )

    def get_pending_tool_approval(
        self,
        _session_id: str,
        _user_id: str | None = None,
    ) -> PendingToolApproval | None:
        return self.pending if self.run_started.is_set() else None


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


async def _post_stream_from(
    app: FastAPI, body: dict[str, str], from_sequence: int
) -> list[dict[str, Any]]:
    """POST /chat/stream?from_sequence=... 并解析全部 SSE 帧(D1-T3 回放测试)。"""
    transport = ASGITransport(app=app)
    frames: list[dict[str, Any]] = []
    async with (
        AsyncClient(transport=transport, base_url="http://testserver") as client,
        client.stream(
            "POST",
            f"/chat/stream?from_sequence={from_sequence}",
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

    # thinking 是可审计的阶段摘要，不是模型隐藏推理原文。
    assert frames[0]["content"] == "正在分析问题并规划协作"
    assert frames[0]["agent"] == "supervisor"


def test_native_stream_yields_text_delta_before_the_graph_finishes(
    tmp_path: Path,
) -> None:
    """reasoning 与正文 token 都在图结束前到达，且互不混入。"""
    graph = NativeTokenStreamingChatGraph()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    payload = ChatRequest(session_id="session-1", message="解释流式")

    class _ConnectedRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def scenario() -> list[dict[str, Any]]:
        lock = asyncio.Lock()
        generator = _stream_events(
            graph,
            store,
            payload,
            _ConnectedRequest(),  # type: ignore[arg-type] - 测试替身只实现所需方法
            "user-1",
            lock,
        )
        frames: list[dict[str, Any]] = []
        try:
            while not any(frame["event_type"] == "message_delta" for frame in frames):
                raw = await asyncio.wait_for(anext(generator), timeout=1)
                if raw.startswith("data: "):
                    frames.append(json.loads(raw[len("data: "):]))
            assert graph.finished.is_set() is False
            assert frames[-1]["content"] == "流"
            graph.release.set()
            async for raw in generator:
                if raw.startswith("data: "):
                    frames.append(json.loads(raw[len("data: "):]))
        finally:
            graph.release.set()
            await generator.aclose()
        return frames

    try:
        caller_thread = threading.get_ident()
        frames = asyncio.run(scenario())
    finally:
        store.close()

    assert graph.stream_thread_id is not None
    assert graph.stream_thread_id != caller_thread
    assert [frame["event_type"] for frame in frames] == [
        "thinking",
        "reasoning",
        "message_delta",
        "message_delta",
        "message_end",
        "done",
    ]
    reasoning = next(frame for frame in frames if frame["event_type"] == "reasoning")
    assert reasoning["content"] == "先识别问题，再组织解释"
    assert reasoning["message_id"] == "assistant-turn"
    assert reasoning["is_delta"] is True
    assert all(
        "先识别问题，再组织解释" not in (frame.get("content") or "")
        for frame in frames
        if frame["event_type"] in {"message_delta", "message_end"}
    )


def test_native_stream_exposes_checkpoint_run_error_instead_of_silent_done(
    tmp_path: Path,
) -> None:
    """模型失败写入 run_error 时，SSE 必须显式报错，不能伪装成功结束。"""
    graph = NativeTerminalStateChatGraph(
        {
            "messages": [HumanMessage(content="解释流式")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": RunError(
                error_code=ErrorCode.MODEL_CALL_FAILED,
                message="provider connection failed",
                agent="supervisor",
            ),
        }
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(
                app,
                {"session_id": "session-1", "message": "解释流式"},
            )
        )
    finally:
        store.close()

    assert [frame["event_type"] for frame in frames] == [
        "thinking",
        "error",
        "done",
    ]
    assert frames[-2]["error_code"] == "model_call_failed"
    assert frames[-2]["agent"] == "supervisor"
    assert "provider connection failed" not in json.dumps(frames)


def test_native_stream_without_answer_or_run_error_reports_internal_error(
    tmp_path: Path,
) -> None:
    """成功终态必须有权威回答；空终态不能只发 done 吞掉异常。"""
    graph = NativeTerminalStateChatGraph(
        {
            "messages": [HumanMessage(content="解释流式")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
        }
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(
                app,
                {"session_id": "session-1", "message": "解释流式"},
            )
        )
    finally:
        store.close()

    assert [frame["event_type"] for frame in frames] == [
        "thinking",
        "error",
        "done",
    ]
    assert frames[-2]["error_code"] == "internal_error"


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


def test_stream_tool_events_carry_redacted_input_and_output_summaries(
    tmp_path: Path,
) -> None:
    """工具详情可展示，但只通过有界、脱敏的摘要字段公开。"""
    events = [
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=0,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            tool_call_id="call-search-1",
            input_summary='{"query":"反向传播","api_key":"[REDACTED]"}',
        ),
        RunEvent(
            event_type=EventType.TOOL_COMPLETED,
            sequence=1,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            tool_call_id="call-search-1",
            success=True,
            output_summary='{"found":true,"hits":2}',
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
            "run_id",
            "agent",
        "tool_name",
        "success",
        "duration_ms",
        "error_code",
        "plan_step_sequence",
        "content",
        "message_id",
        "message",
        "citations",
        "current_agent",
        "tool_call_id",
        "parent_tool_call_id",
        "input_summary",
            "output_summary",
            "output_stream",
            "is_delta",
            "pending_tool_approval",
        }
    for frame in frames:
        assert set(frame).issubset(allowed_keys)
    tool_frames = [
        frame for frame in frames if frame["event_type"] in {"tool_call", "tool_result"}
    ]
    assert len(tool_frames) == 2
    for frame in tool_frames:
        assert frame["tool_name"] == "search_knowledge"
        assert frame["tool_call_id"] == "call-search-1"
    assert tool_frames[0]["input_summary"] == (
        '{"query":"反向传播","api_key":"[REDACTED]"}'
    )
    assert tool_frames[1]["output_summary"] == '{"found":true,"hits":2}'
    assert "secret" not in json.dumps(tool_frames)


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


# ── D1-T3 断线重连与消息补发:from_sequence 回放 ─────────────────────


def test_stream_replays_remaining_events_when_round_finished(
    tmp_path: Path,
) -> None:
    """断线重连:最近一轮已结束且存在比 from_sequence 新的事件 → 回放 + done。

    回放分支在 _ensure_session 后立即执行、不启动 run:断言 graph.run
    未被调用(run_inputs 为空);推送的帧 sequence 全部大于 from_sequence,
    终态映射事件(message_end/done 映射)被跳过,最后补发权威全文与 done
    (sequence 递增)——token 增量本身不落 checkpoint,重连不能只补 done。
    """
    final_events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=3,
            session_id="session-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=4,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
        ),
        RunEvent(
            event_type=EventType.AGENT_COMPLETED,
            sequence=5,
            session_id="session-1",
            agent="supervisor",
            success=True,
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=6,
            session_id="session-1",
            success=True,
        ),
    ]
    final_state = {
        "messages": [HumanMessage(content="请继续"), AIMessage(content="complete")],
        "events": final_events,
        "current_agent": "supervisor",
    }
    graph = StreamingChatGraph(
        final_state,
        # 回放检查发生在 run 启动前(替身此时返回 initial_state):
        # 必须让 initial_state 也是「已结束的终态」,与真实 checkpoint
        # 一致(上一轮已完成、事件已落盘)。
        initial_state=final_state,
        run_delay=0.0,
    )
    app, store = _chat_app(tmp_path, graph)
    existing = store.create_session("session-1", user_id="user-1")
    try:
        frames = asyncio.run(
            _post_stream_from(
                app,
                {"session_id": "session-1", "message": "请继续"},
                from_sequence=3,
            )
        )
        replayed = store.list_sessions(user_id="user-1")[0]
    finally:
        store.close()

    # 回放不启动 run(断线重连避免整轮重复执行)。
    assert graph.run_inputs == []
    assert replayed.updated_at == existing.updated_at

    # 只回放 sequence > 3 的公开事件;AGENT_COMPLETED(message_end 映射)
    # 与 RUN_COMPLETED(done 映射)被跳过,随后补权威 message_end + done。
    assert [frame["event_type"] for frame in frames] == [
        "tool_call",
        "message_end",
        "done",
    ]
    assert frames[-2]["content"] == "complete"
    # 公开事件 4(TOOL_STARTED)先推;合成帧的 sequence 必须大于「真实
    # 终态序列」(终态 RUN_COMPLETED 是 6,故 message_end = 7,done = 8):
    # 公开序列可能有间隙,用 last_sequence+1 会让 done 小于真实终态,
    # 调用方把 done 传回时下一条消息会被误判为回放)。
    assert [frame["sequence"] for frame in frames] == [4, 7, 8]
    assert all(frame["sequence"] > 3 for frame in frames)
    assert frames[-1]["event_type"] == "done"


def test_native_stream_pauses_with_an_exact_tool_approval_instead_of_an_error(
    tmp_path: Path,
) -> None:
    graph = NativePendingToolChatGraph()
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(
                app,
                {"session_id": "session-1", "message": "inspect"},
            )
        )
    finally:
        store.close()

    assert [frame["event_type"] for frame in frames] == [
        "thinking",
        "approval_required",
        "done",
    ]
    approval = frames[1]["pending_tool_approval"]
    assert approval["interrupt_id"] == "interrupt-shell-1"
    assert approval["request"]["arguments"]["command"] == "git status"
    assert all(frame["event_type"] != "error" for frame in frames)


def test_stream_reconnect_with_token_sequence_above_checkpoint_does_not_rerun(
    tmp_path: Path,
) -> None:
    """token 帧序号可高于 core 终态；重连仍应补全文而不是重跑代理。"""
    final_events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=0,
            session_id="session-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=2,
            session_id="session-1",
            success=True,
        ),
    ]
    final_state = {
        "messages": [HumanMessage(content="解释"), AIMessage(content="权威完整回答")],
        "events": final_events,
        "current_agent": "supervisor",
    }
    graph = StreamingChatGraph(
        final_state,
        initial_state=final_state,
        run_delay=0.0,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream_from(
                app,
                {"session_id": "session-1", "message": "解释"},
                from_sequence=40,
            )
        )
    finally:
        store.close()

    assert graph.run_inputs == []
    assert [frame["event_type"] for frame in frames] == ["message_end", "done"]
    assert [frame["sequence"] for frame in frames] == [41, 42]
    assert frames[0]["content"] == "权威完整回答"


def test_stream_reconnect_never_reruns_when_round_not_finished(
    tmp_path: Path,
) -> None:
    """重连时最近一轮未结束也不得重跑同一输入。

    真实故障链路是：长任务仍在后台执行时浏览器断线，续传请求带正数
    from_sequence 到达；此时 checkpoint 可能只落了中间事件。把它当成
    新消息会让同一子代理任务执行两次。正数游标只表示续传，绝不具有
    启动新 run 的权限；连接可无事件结束，由客户端稍后再次续传。
    """
    final_events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=3,
            session_id="session-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=4,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
        ),
        RunEvent(
            event_type=EventType.TOOL_COMPLETED,
            sequence=5,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            success=True,
        ),
    ]
    graph = StreamingChatGraph(
        {
            "messages": [HumanMessage(content="请检索"), AIMessage(content="检索完成")],
            "events": final_events,
            "current_agent": "supervisor",
        },
        # 与既有测试同款:中间态与最终态同事件集,消除轮询竞态。
        intermediate_state={
            "events": final_events,
            "messages": [HumanMessage(content="请检索"), AIMessage(content="检索完成")],
        },
        run_delay=0.05,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream_from(
                app,
                {"session_id": "session-1", "message": "请检索"},
                from_sequence=2,
            )
        )
    finally:
        store.close()

    assert graph.run_inputs == []
    assert frames == []


def test_stream_from_sequence_zero_starts_new_run_when_round_finished(
    tmp_path: Path,
) -> None:
    """正常发新消息(from_sequence=0)时,即使最近一轮已结束也必须启动新 run。

    core 的 events 通道跨轮累积(sequence 全局递增),若回放条件对
    from_sequence=0 也生效,第二条消息会被误判为断线重连、回放上一轮
    而永远无法启动新 run——因此回放只对 from_sequence > 0 生效。
    本测试用「initial_state 已是终态轮次」的替身锁住该边界。
    """
    final_events = [
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
    final_state = {
        "messages": [HumanMessage(content="第一轮"), AIMessage(content="done")],
        "events": final_events,
        "current_agent": "supervisor",
    }
    # initial_state 是「上一轮已结束」的 checkpoint(事件含终态,消息只有
    # 上一轮的用户消息);run 完成后 final_state 多出本轮助手消息,收尾
    # 合成 message_end(与既有测试的 previous_count 语义一致)。
    initial_state = {
        "messages": [HumanMessage(content="第一轮")],
        "events": final_events,
        "current_agent": "supervisor",
    }
    graph = StreamingChatGraph(
        final_state,
        initial_state=initial_state,
        intermediate_state=initial_state,
        run_delay=0.05,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(app, {"session_id": "session-1", "message": "第二轮"})
        )
    finally:
        store.close()

    # from_sequence=0(默认):即使 checkpoint 已存在已结束的轮次,
    # 也启动新 run(不把上一轮误判为重连回放)。
    assert graph.run_inputs == [("第二轮", "session-1", "user-1")]
    assert frames[-1]["event_type"] == "done"
    # 增量推送从上一轮最后 sequence(1)之后开始:message_end + done。
    assert [frame["event_type"] for frame in frames] == ["message_end", "done"]


def test_stream_replay_done_sequence_exceeds_real_terminal_with_public_gap(
    tmp_path: Path,
) -> None:
    """公开事件序列有间隙时,回放 done 的 sequence 仍大于真实终态序列。

    review 修正:core 的 events 通道存在被 EVENT_TYPE_MAP 过滤的事件
    (如 TASK_RESULTS_AGGREGATED),公开序列因此有间隙;若回放 done 用
    「最后一个公开事件 + 1」,可能小于真实终态序列——调用方把 done
    的 sequence 传回重连时,回放条件「终态 > from_sequence」仍然成立,
    下一条消息会被误判为回放而静默吞掉。本测试构造 6 号事件被过滤的
    场景,断言 done.sequence = 真实终态 + 1。
    """
    final_events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=3,
            session_id="session-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=4,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
        ),
        # 5 号是 TASK_RESULTS_AGGREGATED(不在 EVENT_TYPE_MAP,公开时被过滤)
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=6,
            session_id="session-1",
            success=True,
        ),
    ]
    final_state = {
        "messages": [HumanMessage(content="请继续"), AIMessage(content="complete")],
        "events": final_events,
        "current_agent": "supervisor",
    }
    graph = StreamingChatGraph(
        final_state,
        initial_state=final_state,
        run_delay=0.0,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream_from(
                app,
                {"session_id": "session-1", "message": "请继续"},
                from_sequence=3,
            )
        )
    finally:
        store.close()

    # 公开事件只有 4(TOOL_STARTED);随后补权威全文与 done。
    assert [frame["event_type"] for frame in frames] == [
        "tool_call",
        "message_end",
        "done",
    ]
    assert [frame["sequence"] for frame in frames] == [4, 7, 8]

def test_stream_message_end_carries_citations_when_final_message_has_them(
    tmp_path: Path,
) -> None:
    """D3-T5:流式主通道的引用由 message_end 事件携带(review blocking 修复)。

    后端在合成 message_end 时复用 _response_references(与 POST /chat
    的 references 同源,两级口径:优先最终消息自身引用,聚合回答无
    引用时回退本轮最近带引用的 worker 作答);前端在 message_end 事件
    读取 citations 存入 store。
    """
    final_message = AIMessage(
        content="评估完成",
        # 键必须用 core 的 REFERENCES_METADATA_KEY("references"),
        # 与 _attach_references / _api_citations 的读写一致
        # (test_chat_api 同款口径)。
        additional_kwargs={
            "references": [
                {
                    "document_id": "ml-zhouzhihua",
                    "source": "ml-zhouzhihua",
                    "page": 88,
                    "chunk_id": "ml-zhouzhihua:88:0:500",
                }
            ]
        },
    )
    final_events = [
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
            "messages": [HumanMessage(content="请评估"), final_message],
            "events": final_events,
            "current_agent": "supervisor",
        },
        intermediate_state={"events": [], "messages": []},
        run_delay=0.0,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        frames = asyncio.run(
            _post_stream(app, {"session_id": "session-1", "message": "请评估"})
        )
    finally:
        store.close()

    message_end_frames = [
        frame for frame in frames if frame["event_type"] == "message_end"
    ]
    assert message_end_frames
    assert message_end_frames[-1]["citations"] == [
        {
            "document_id": "ml-zhouzhihua",
            "source": "ml-zhouzhihua",
            "page": 88,
            "chunk_id": "ml-zhouzhihua:88:0:500",
        }
    ]
    assert frames[-1]["event_type"] == "done"
