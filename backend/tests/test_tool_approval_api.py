"""REST boundary tests for approval-gated tool calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from api.app import create_app
from core.events import EventType, RunEvent
from core.sessions import SessionStore
from core.state import (
    AgentRole,
    PendingToolApproval,
    ToolApprovalAction,
    ToolApprovalDecision,
    ToolApprovalRequest,
)


class ToolApprovalGraph:
    def __init__(self, pending: PendingToolApproval | None) -> None:
        self.pending = pending
        self.decisions: list[ToolApprovalDecision] = []

    def get_pending_tool_approval(
        self,
        _session_id: str,
        _user_id: str | None = None,
    ) -> PendingToolApproval | None:
        return self.pending

    def get_state(
        self,
        _session_id: str,
        _user_id: str | None = None,
    ) -> dict[str, Any]:
        return {"messages": [HumanMessage(content="inspect")], "events": []}

    def resume_tool_approval(
        self,
        _session_id: str,
        decision: ToolApprovalDecision,
        _user_id: str | None = None,
    ) -> dict[str, Any]:
        self.decisions.append(decision)
        self.pending = None
        return {
            "current_agent": "supervisor",
            "events": [],
            "messages": [
                HumanMessage(content="inspect"),
                AIMessage(content="command completed"),
            ],
        }


class StreamingToolApprovalGraph(ToolApprovalGraph):
    def __init__(self, pending: PendingToolApproval) -> None:
        super().__init__(pending)
        self.state: dict[str, Any] = {
            "run_id": "run-1",
            "current_agent": "supervisor",
            "messages": [HumanMessage(content="inspect")],
            "events": [],
        }

    def get_state(
        self,
        _session_id: str,
        _user_id: str | None = None,
    ) -> dict[str, Any]:
        return self.state

    def stream_tool_approval(
        self,
        session_id: str,
        decision: ToolApprovalDecision,
        _user_id: str | None = None,
    ):
        self.decisions.append(decision)
        self.pending = None
        output = RunEvent(
            event_type=EventType.TOOL_OUTPUT,
            sequence=0,
            session_id=session_id,
            run_id="run-1",
            agent="supervisor",
            tool_name="shell",
            tool_call_id="shell-1",
            content="first\n",
            output_stream="stdout",
        )
        yield "custom", {
            "kind": "run_event",
            "event": output.model_dump(mode="json"),
        }
        yield "messages", (
            AIMessageChunk(content="command completed", id="answer-1"),
            {"agent_role": "supervisor"},
        )
        self.state = {
            "run_id": "run-1",
            "current_agent": "supervisor",
            "events": [output],
            "messages": [
                HumanMessage(content="inspect"),
                AIMessage(content="command completed"),
            ],
        }


def _pending() -> PendingToolApproval:
    return PendingToolApproval(
        interrupt_id="interrupt-tool-1",
        request=ToolApprovalRequest(
            tool_call_id="shell-1",
            tool_name="shell",
            agent_role=AgentRole.SUPERVISOR,
            arguments={
                "command": "git status; git diff --stat",
                "cwd": "D:\\Projects\\course",
                "description": "inspect changes",
                "timeout_seconds": 30,
            },
        ),
    )


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: dict[str, str] | None = None,
    user_id: str = "user-1",
):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.request(
            method,
            path,
            headers={"X-User-Id": user_id},
            json=json,
        )


def test_get_pending_tool_approval_exposes_the_exact_validated_call(
    tmp_path: Path,
) -> None:
    graph = ToolApprovalGraph(_pending())
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.graph = graph
    app.state.session_store = store
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(app, "GET", "/sessions/session-1/tool-approval")
        )
    finally:
        store.close()

    assert response.status_code == 200
    payload = response.json()["pending_tool_approval"]
    assert payload["interrupt_id"] == "interrupt-tool-1"
    assert payload["request"]["tool_name"] == "shell"
    assert payload["request"]["arguments"]["command"] == "git status; git diff --stat"


def test_confirm_tool_approval_resumes_the_exact_interrupt(tmp_path: Path) -> None:
    graph = ToolApprovalGraph(_pending())
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.graph = graph
    app.state.session_store = store
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions/session-1/tool-approval",
                json={"interrupt_id": "interrupt-tool-1", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["message"]["content"] == "command completed"
    assert graph.decisions == [
        ToolApprovalDecision(
            interrupt_id="interrupt-tool-1",
            action=ToolApprovalAction.CONFIRM,
        )
    ]


def test_tool_approval_rejects_a_stale_or_missing_interrupt(tmp_path: Path) -> None:
    graph = ToolApprovalGraph(_pending())
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.graph = graph
    app.state.session_store = store
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions/session-1/tool-approval",
                json={"interrupt_id": "stale", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "tool_approval_not_pending"
    assert graph.decisions == []


def test_confirm_tool_approval_streams_terminal_output_before_final_answer(
    tmp_path: Path,
) -> None:
    graph = StreamingToolApprovalGraph(_pending())
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.graph = graph
    app.state.session_store = store
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions/session-1/tool-approval/stream",
                json={"interrupt_id": "interrupt-tool-1", "action": "confirm"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [event["event_type"] for event in events] == [
        "tool_output",
        "message_delta",
        "message_end",
        "done",
    ]
    assert events[0]["tool_call_id"] == "shell-1"
    assert events[0]["output_stream"] == "stdout"
    assert events[0]["content"] == "first\n"
    assert events[2]["message"]["content"] == "command completed"
