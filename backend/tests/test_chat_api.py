"""同步聊天 REST API 测试。"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from langchain_core.messages import AIMessage, HumanMessage

from api.app import create_app
from core.events import ErrorCode, EventType, RunError, RunEvent
from core.sessions import SessionStore
from core.state import AgentRole, HandoffApprovalRequest, PendingHandoffApproval


class ChatGraph:
    """为 API 测试提供可控的同步图替身。"""

    def __init__(
        self,
        state: dict[str, Any],
        *,
        previous_state: dict[str, Any] | None = None,
        pending_handoff: PendingHandoffApproval | None = None,
        run_exception: Exception | None = None,
    ) -> None:
        self._state = state
        self._previous_state = previous_state
        self._pending_handoff = pending_handoff
        self._run_exception = run_exception
        self.run_thread_id: int | None = None
        self.run_inputs: list[tuple[str, str, str | None]] = []

    def get_state(self, session_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self._previous_state

    def run(self, user_input: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.run_thread_id = threading.get_ident()
        self.run_inputs.append((user_input, session_id, user_id))
        if self._run_exception is not None:
            raise self._run_exception
        return self._state

    def get_pending_handoff(
        self, session_id: str, user_id: str | None = None
    ) -> PendingHandoffApproval | None:
        return self._pending_handoff


class BlockingChatGraph(ChatGraph):
    """A graph substitute that keeps one run active for concurrency tests."""

    def __init__(self) -> None:
        super().__init__(
            {
                "messages": [AIMessage(content="complete")],
                "events": [],
                "current_agent": "supervisor",
                "run_error": None,
                "pending_handoff": None,
            }
        )
        self.run_started = threading.Event()
        self.release_run = threading.Event()

    def run(self, user_input: str, session_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.run_started.set()
        if not self.release_run.wait(timeout=2):
            raise TimeoutError("test run was not released")
        return super().run(user_input, session_id, user_id)


def _chat_app(tmp_path: Path, graph: ChatGraph) -> tuple[FastAPI, SessionStore]:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    app.state.graph = graph
    return app, store


async def _post_chat(app: FastAPI, body: dict[str, str]) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/chat", headers={"X-User-Id": "user-1"}, json=body)


def test_chat_creates_missing_session_runs_in_worker_and_returns_event_delta(tmp_path: Path) -> None:
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    result_events = [
        previous_event,
        RunEvent(
            event_type=EventType.AGENT_COMPLETED,
            sequence=1,
            session_id="session-1",
            agent="evaluator",
            success=True,
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=2,
            session_id="session-1",
            agent=None,
            success=True,
        ),
    ]
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请评估"), AIMessage(content="评估完成")],
            "events": result_events,
            "current_agent": "evaluator",
            "run_error": None,
            "pending_handoff": None,
        },
        previous_state={"events": [previous_event]},
    )
    app, store = _chat_app(tmp_path, graph)
    caller_thread = threading.get_ident()
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请评估"}))
        sessions = store.list_sessions(user_id="user-1")
    finally:
        store.close()

    assert response.status_code == 200
    assert graph.run_thread_id is not None
    assert graph.run_thread_id != caller_thread
    assert graph.run_inputs == [("请评估", "session-1", "user-1")]
    assert [event["sequence"] for event in response.json()["events"]] == [1, 2]
    assert [event["event_type"] for event in response.json()["events"]] == [
        "message_end",
        "done",
    ]
    assert response.json()["message"] == {
        "role": "assistant",
        "content": "评估完成",
        "agent": "evaluator",
        "created_at": None,
    }
    assert response.json()["current_agent"] == "evaluator"
    assert [session.session_id for session in sessions] == ["session-1"]


def test_chat_returns_run_errors_as_a_successful_contract_response(tmp_path: Path) -> None:
    previous_messages = [
        HumanMessage(content="earlier request"),
        AIMessage(content="earlier answer"),
    ]
    graph = ChatGraph(
        {
            "messages": previous_messages,
            "events": [
                RunEvent(
                    event_type=EventType.RUN_FAILED,
                    sequence=0,
                    session_id="session-1",
                    agent="supervisor",
                    success=False,
                    error_code=ErrorCode.MODEL_CALL_FAILED,
                )
            ],
            "current_agent": "supervisor",
            "run_error": RunError(
                error_code=ErrorCode.MODEL_CALL_FAILED,
                message="internal secret details",
                agent="supervisor",
            ),
            "pending_handoff": None,
        },
        previous_state={"messages": previous_messages},
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请评估"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["message"] is None
    assert response.json()["run_error"] == {
        "error_code": "model_call_failed",
        "message": "The request could not be completed.",
        "agent": "supervisor",
    }
    assert response.json()["events"][0]["event_type"] == "error"
    assert "internal secret details" not in response.text
    assert "earlier answer" not in response.text


def test_chat_returns_pending_handoff_when_the_graph_pauses(tmp_path: Path) -> None:
    pending_handoff = PendingHandoffApproval(
        interrupt_id="interrupt-1",
        request=HandoffApprovalRequest(
            target_agent=AgentRole.TEACHING_ASSISTANT,
            task_content="检查课程设计",
            plan_step_sequence=1,
        ),
    )
    graph = ChatGraph(
        {
            "messages": [HumanMessage(content="请检查")],
            "events": [],
            "current_agent": "supervisor",
            "run_error": None,
            "pending_handoff": {"ignored": "use public accessor"},
        },
        pending_handoff=pending_handoff,
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请检查"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["pending_handoff"] == {
        "interrupt_id": "interrupt-1",
        "request": {
            "target_agent": "teaching_assistant",
            "task_content": "检查课程设计",
            "plan_step_sequence": 1,
        },
    }


def test_chat_reports_a_pending_session_without_a_500(tmp_path: Path) -> None:
    graph = ChatGraph(
        {},
        run_exception=RuntimeError("存在待恢复执行，请先调用 resume_handoff()"),
    )
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "请继续"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["run_error"] == {
        "error_code": "session_busy",
        "message": "The session is waiting for a pending operation.",
        "agent": None,
    }


def test_chat_rejects_a_concurrent_request_for_the_same_session(tmp_path: Path) -> None:
    graph = BlockingChatGraph()
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def send_concurrent_requests() -> tuple[Response, Response]:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first_task = asyncio.create_task(
                client.post(
                    "/chat",
                    headers={"X-User-Id": "user-1"},
                    json={"session_id": "session-1", "message": "first"},
                )
            )
            await asyncio.wait_for(asyncio.to_thread(graph.run_started.wait), timeout=1)
            second_task = asyncio.create_task(
                client.post(
                    "/chat",
                    headers={"X-User-Id": "user-1"},
                    json={"session_id": "session-1", "message": "second"},
                )
            )
            try:
                await asyncio.sleep(0.05)
                assert second_task.done()
                second_response = second_task.result()
            finally:
                graph.release_run.set()
                first_response = await first_task
                if not second_task.done():
                    await second_task
            return first_response, second_response

    try:
        first_response, second_response = asyncio.run(send_concurrent_requests())
    finally:
        store.close()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["run_error"] == {
        "error_code": "session_busy",
        "message": "Another request is already running for this session.",
        "agent": None,
    }
    assert second_response.json()["events"] == []
    assert graph.run_inputs == [("first", "session-1", "user-1")]


def test_chat_does_not_misclassify_an_unexpected_runtime_error(tmp_path: Path) -> None:
    graph = ChatGraph({}, run_exception=RuntimeError("internal invariant failed"))
    app, store = _chat_app(tmp_path, graph)
    try:
        response = asyncio.run(_post_chat(app, {"session_id": "session-1", "message": "go"}))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["run_error"] == {
        "error_code": "internal_error",
        "message": "The request could not be completed.",
        "agent": None,
    }
    assert "internal invariant failed" not in response.text


def test_invalid_chat_request_does_not_echo_message_content(tmp_path: Path) -> None:
    graph = ChatGraph({})
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def send_invalid_request() -> Response:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/chat",
                headers={"X-User-Id": "user-1"},
                json={"session_id": "session-1", "message": ""},
            )

    try:
        response = asyncio.run(send_invalid_request())
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert '"input"' not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": " ", "message": "valid message"},
        {"session_id": "session-1", "message": " "},
    ],
)
def test_whitespace_chat_fields_are_rejected_before_graph_execution(
    tmp_path: Path, body: dict[str, str]
) -> None:
    graph = ChatGraph({})
    app, store = _chat_app(tmp_path, graph)
    transport = ASGITransport(app=app)

    async def send_invalid_request() -> Response:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/chat",
                headers={"X-User-Id": "user-1"},
                json=body,
            )

    try:
        response = asyncio.run(send_invalid_request())
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert graph.run_inputs == []
