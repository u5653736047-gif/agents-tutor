"""Minimal handoff-approval REST API tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from langchain_core.messages import AIMessage, HumanMessage

from api.app import create_app
from core.events import EventType, RunEvent
from core.knowledge.models import Citation as CoreCitation
from core.sessions import SessionStore
from core.state import (
    REFERENCES_METADATA_KEY,
    AgentRole,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    HandoffApprovalRequest,
    PendingHandoffApproval,
)


class ApprovalGraph:
    """Controllable synchronous graph substitute for approval-route tests."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        previous_state: dict[str, Any] | None = None,
        pending_handoff: PendingHandoffApproval | None = None,
        pending_after_resume: PendingHandoffApproval | None = None,
        resume_exception: Exception | None = None,
    ) -> None:
        self._state = {} if state is None else state
        self._previous_state = previous_state
        self._pending_handoff = pending_handoff
        self._pending_after_resume = pending_after_resume
        self._resume_exception = resume_exception
        self.resume_thread_id: int | None = None
        self.resume_inputs: list[tuple[str, HandoffApprovalDecision, str | None]] = []

    def get_state(self, session_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        return self._previous_state

    def get_pending_handoff(
        self, session_id: str, user_id: str | None = None
    ) -> PendingHandoffApproval | None:
        return self._pending_handoff

    def resume_handoff(
        self,
        session_id: str,
        decision: HandoffApprovalDecision,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.resume_thread_id = threading.get_ident()
        self.resume_inputs.append((session_id, decision, user_id))
        if self._resume_exception is not None:
            raise self._resume_exception
        self._pending_handoff = self._pending_after_resume
        return self._state


def _approval_app(tmp_path: Path, graph: ApprovalGraph) -> tuple[FastAPI, SessionStore]:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    app.state.graph = graph
    return app, store


async def _get_pending(app: FastAPI, session_id: str, user_id: str = "user-1") -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(
            f"/sessions/{session_id}/handoff",
            headers={"X-User-Id": user_id},
        )


async def _submit_decision(
    app: FastAPI,
    session_id: str,
    payload: dict[str, str],
    user_id: str = "user-1",
) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            f"/sessions/{session_id}/handoff",
            headers={"X-User-Id": user_id},
            json=payload,
        )


def _pending_handoff() -> PendingHandoffApproval:
    return PendingHandoffApproval(
        interrupt_id="interrupt-1",
        request=HandoffApprovalRequest(
            target_agent=AgentRole.TEACHING_ASSISTANT,
            task_content="review the lesson plan",
            plan_step_sequence=1,
        ),
    )


def test_get_pending_handoff_returns_the_current_public_request(tmp_path: Path) -> None:
    graph = ApprovalGraph(pending_handoff=_pending_handoff())
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(_get_pending(app, "session-1"))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session-1",
        "pending_handoff": {
            "interrupt_id": "interrupt-1",
            "request": {
                "target_agent": "teaching_assistant",
                "task_content": "review the lesson plan",
                "plan_step_sequence": 1,
            },
        },
    }


def test_approval_without_a_pending_handoff_returns_a_stable_error(tmp_path: Path) -> None:
    graph = ApprovalGraph()
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error_code": "handoff_not_pending",
            "message": "No handoff is pending for this session.",
        }
    }
    assert graph.resume_inputs == []


def test_handoff_routes_hide_sessions_owned_by_another_user(tmp_path: Path) -> None:
    graph = ApprovalGraph(pending_handoff=_pending_handoff())
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="owner")
    try:
        pending_response = asyncio.run(_get_pending(app, "session-1", user_id="other"))
        decision_response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "confirm"},
                user_id="other",
            )
        )
    finally:
        store.close()

    expected = {
        "detail": {
            "error_code": "session_not_found",
            "message": "Session was not found.",
        }
    }
    assert pending_response.status_code == 404
    assert pending_response.json() == expected
    assert decision_response.status_code == 404
    assert decision_response.json() == expected
    assert graph.resume_inputs == []


def test_approval_sanitizes_a_resume_race_without_a_pending_handoff(tmp_path: Path) -> None:
    graph = ApprovalGraph(
        pending_handoff=_pending_handoff(),
        resume_exception=ValueError("interrupt no longer exists"),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error_code": "handoff_not_pending",
            "message": "No handoff is pending for this session.",
        }
    }
    assert len(graph.resume_inputs) == 1
    assert "interrupt no longer exists" not in response.text


def test_approval_rejects_a_stale_interrupt_id_without_resuming(tmp_path: Path) -> None:
    graph = ApprovalGraph(pending_handoff=_pending_handoff())
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "stale-interrupt", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "error_code": "handoff_not_pending",
            "message": "No handoff is pending for this session.",
        }
    }
    assert graph.resume_inputs == []


def test_confirm_handoff_resumes_in_a_worker_and_returns_chat_response(tmp_path: Path) -> None:
    previous_message = HumanMessage(content="original request")
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    graph = ApprovalGraph(
        {
            "messages": [previous_message, AIMessage(content="continued answer")],
            "events": [
                previous_event,
                RunEvent(
                    event_type=EventType.AGENT_SWITCHED,
                    sequence=1,
                    session_id="session-1",
                    agent="teaching_assistant",
                    success=True,
                ),
                RunEvent(
                    event_type=EventType.RUN_COMPLETED,
                    sequence=2,
                    session_id="session-1",
                    success=True,
                ),
            ],
            "current_agent": "teaching_assistant",
            "run_error": None,
        },
        previous_state={"messages": [previous_message], "events": [previous_event]},
        pending_handoff=_pending_handoff(),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    caller_thread = threading.get_ident()
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert graph.resume_thread_id is not None
    assert graph.resume_thread_id != caller_thread
    assert len(graph.resume_inputs) == 1
    _, decision, user_id = graph.resume_inputs[0]
    assert decision.interrupt_id == "interrupt-1"
    assert decision.action is HandoffApprovalAction.CONFIRM
    assert user_id == "user-1"
    assert response.json()["message"] == {
        "role": "assistant",
        "content": "continued answer",
        "agent": "teaching_assistant",
        "created_at": None,
    }
    assert [event["sequence"] for event in response.json()["events"]] == [1, 2]
    assert response.json()["pending_handoff"] is None


def test_reject_handoff_terminates_without_returning_a_previous_message(tmp_path: Path) -> None:
    previous_messages = [
        HumanMessage(content="original request"),
        AIMessage(content="previous answer"),
    ]
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    graph = ApprovalGraph(
        {
            "messages": previous_messages,
            "events": [
                previous_event,
                RunEvent(
                    event_type=EventType.RUN_COMPLETED,
                    sequence=1,
                    session_id="session-1",
                    agent="supervisor",
                    success=True,
                ),
            ],
            "current_agent": "supervisor",
            "run_error": None,
        },
        previous_state={"messages": previous_messages, "events": [previous_event]},
        pending_handoff=_pending_handoff(),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "reject"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert graph.resume_inputs[0][1].action is HandoffApprovalAction.REJECT
    assert response.json()["message"] is None
    assert [event["event_type"] for event in response.json()["events"]] == ["done"]
    assert "previous answer" not in response.text


def test_approval_rejects_reserved_modification_fields(tmp_path: Path) -> None:
    graph = ApprovalGraph(pending_handoff=_pending_handoff())
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {
                    "interrupt_id": "interrupt-1",
                    "action": "confirm",
                    "task_content": "modified content",
                },
            )
        )
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert graph.resume_inputs == []


def test_confirm_handoff_response_carries_references_from_the_final_message(
    tmp_path: Path,
) -> None:
    """审批确认路径复用 chat_response_for_state，同样填充 references。"""
    previous_message = HumanMessage(content="original request")
    answer = AIMessage(
        content="continued answer",
        additional_kwargs={
            REFERENCES_METADATA_KEY: [
                CoreCitation(
                    document_id="algebra",
                    source="algebra.txt",
                    page=1,
                    chunk_id="chunk-algebra-1",
                ).model_dump(mode="json")
            ]
        },
    )
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    graph = ApprovalGraph(
        {
            "messages": [previous_message, answer],
            "events": [
                previous_event,
                RunEvent(
                    event_type=EventType.RUN_COMPLETED,
                    sequence=1,
                    session_id="session-1",
                    success=True,
                ),
            ],
            "current_agent": "teaching_assistant",
            "run_error": None,
        },
        previous_state={"messages": [previous_message], "events": [previous_event]},
        pending_handoff=_pending_handoff(),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["references"] == [
        {
            "document_id": "algebra",
            "source": "algebra.txt",
            "page": 1,
            "chunk_id": "chunk-algebra-1",
        }
    ]


# D2-T4:审批修改工作流(修改目标 Agent / 任务内容)————————————————
def _modified_run_state(previous_message: HumanMessage) -> dict[str, Any]:
    """modify 成功路径的替身 state(与 confirm 成功测试同构)。"""
    return {
        "messages": [previous_message, AIMessage(content="continued answer")],
        "events": [
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
        ],
        "current_agent": "learning_assistant",
        "run_error": None,
    }


def test_decide_handoff_modify_target_agent_resumes_with_the_new_target(
    tmp_path: Path,
) -> None:
    """D2-T4:modify 仅改目标 Agent 时,core 决策携带新目标与 action=modify。"""
    previous_message = HumanMessage(content="original request")
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    graph = ApprovalGraph(
        _modified_run_state(previous_message),
        previous_state={"messages": [previous_message], "events": [previous_event]},
        pending_handoff=_pending_handoff(),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {
                    "interrupt_id": "interrupt-1",
                    "action": "modify",
                    "target_agent": "learning_assistant",
                },
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert len(graph.resume_inputs) == 1
    _, decision, user_id = graph.resume_inputs[0]
    assert decision.action is HandoffApprovalAction.MODIFY
    assert decision.target_agent == AgentRole.LEARNING_ASSISTANT
    assert decision.task_content is None
    assert user_id == "user-1"


def test_decide_handoff_modify_task_content_resumes_with_the_new_content(
    tmp_path: Path,
) -> None:
    """D2-T4:modify 仅改任务内容时,core 决策携带新内容。"""
    previous_message = HumanMessage(content="original request")
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    graph = ApprovalGraph(
        _modified_run_state(previous_message),
        previous_state={"messages": [previous_message], "events": [previous_event]},
        pending_handoff=_pending_handoff(),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {
                    "interrupt_id": "interrupt-1",
                    "action": "modify",
                    "task_content": "rewrite the lesson plan",
                },
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert len(graph.resume_inputs) == 1
    _, decision, _ = graph.resume_inputs[0]
    assert decision.action is HandoffApprovalAction.MODIFY
    assert decision.target_agent is None
    assert decision.task_content == "rewrite the lesson plan"


def test_decide_handoff_modify_carries_both_fields_when_provided(
    tmp_path: Path,
) -> None:
    """D2-T4:modify 同时携带目标与内容时,两个字段都透传给 core。"""
    previous_message = HumanMessage(content="original request")
    previous_event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=0,
        session_id="session-1",
        agent="supervisor",
    )
    graph = ApprovalGraph(
        _modified_run_state(previous_message),
        previous_state={"messages": [previous_message], "events": [previous_event]},
        pending_handoff=_pending_handoff(),
    )
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {
                    "interrupt_id": "interrupt-1",
                    "action": "modify",
                    "target_agent": "learning_assistant",
                    "task_content": "rewrite the lesson plan",
                },
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert len(graph.resume_inputs) == 1
    _, decision, _ = graph.resume_inputs[0]
    assert decision.action is HandoffApprovalAction.MODIFY
    assert decision.target_agent == AgentRole.LEARNING_ASSISTANT
    assert decision.task_content == "rewrite the lesson plan"


def test_decide_handoff_modify_without_changes_is_rejected(tmp_path: Path) -> None:
    """D2-T4:modify 未携带任何修改字段 → 422(双分支第一半)。"""
    graph = ApprovalGraph(pending_handoff=_pending_handoff())
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {"interrupt_id": "interrupt-1", "action": "modify"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert graph.resume_inputs == []


def test_decide_handoff_confirm_with_modification_fields_is_rejected(
    tmp_path: Path,
) -> None:
    """D2-T4:confirm 携带修改字段 → 422(双分支第二半)。"""
    graph = ApprovalGraph(pending_handoff=_pending_handoff())
    app, store = _approval_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _submit_decision(
                app,
                "session-1",
                {
                    "interrupt_id": "interrupt-1",
                    "action": "confirm",
                    "target_agent": "learning_assistant",
                },
            )
        )
    finally:
        store.close()

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert graph.resume_inputs == []
