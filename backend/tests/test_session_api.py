"""会话 REST API 测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from api.app import create_app
from core.sessions import SessionStore


class HistoryGraph:
    """提供会话历史的最小 Graph 替身。"""

    def __init__(self, histories: Mapping[tuple[str, str | None], list[BaseMessage]]) -> None:
        self._histories = histories

    def get_history(self, session_id: str, user_id: str | None = None) -> list[BaseMessage]:
        return self._histories.get((session_id, user_id), [])


def _session_app(tmp_path: Path, graph: HistoryGraph | None = None) -> tuple[FastAPI, SessionStore]:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    app.state.graph = graph or HistoryGraph({})
    return app, store


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: Any,
) -> Response:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def test_session_routes_create_list_and_archive_for_one_user(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(app, "POST", "/sessions", headers=headers, json={"session_id": "session-1"})
        )
        active_sessions = asyncio.run(_request(app, "GET", "/sessions", headers=headers))
        archived = asyncio.run(
            _request(app, "POST", "/sessions/session-1/archive", headers=headers)
        )
        hidden_archived_sessions = asyncio.run(
            _request(app, "GET", "/sessions", headers=headers)
        )
        all_sessions = asyncio.run(
            _request(app, "GET", "/sessions?include_archived=true", headers=headers)
        )
    finally:
        store.close()

    assert created.status_code == 201
    assert created.json()["session_id"] == "session-1"
    assert active_sessions.json() == [created.json()]
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert hidden_archived_sessions.json() == []
    assert all_sessions.json() == [archived.json()]


def test_sessions_are_isolated_and_unknown_sessions_return_404(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    first_user = {"X-User-Id": "user-1"}
    second_user = {"X-User-Id": "user-2"}
    try:
        first_created = asyncio.run(
            _request(app, "POST", "/sessions", headers=first_user, json={"session_id": "shared"})
        )
        second_created = asyncio.run(
            _request(app, "POST", "/sessions", headers=second_user, json={"session_id": "shared"})
        )
        asyncio.run(
            _request(app, "POST", "/sessions", headers=first_user, json={"session_id": "private"})
        )
        denied_archive = asyncio.run(
            _request(app, "POST", "/sessions/private/archive", headers=second_user)
        )
        denied_history = asyncio.run(
            _request(app, "GET", "/sessions/private/messages", headers=second_user)
        )
    finally:
        store.close()

    assert first_created.status_code == 201
    assert second_created.status_code == 201
    assert denied_archive.status_code == 404
    assert denied_history.status_code == 404
    assert denied_archive.json()["detail"]["error_code"] == "session_not_found"
    assert denied_history.json()["detail"]["error_code"] == "session_not_found"


def test_missing_user_header_uses_the_anonymous_session_scope(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    try:
        created = asyncio.run(
            _request(app, "POST", "/sessions", json={"session_id": "anonymous-session"})
        )
        anonymous_sessions = asyncio.run(_request(app, "GET", "/sessions"))
        identified_sessions = asyncio.run(
            _request(app, "GET", "/sessions", headers={"X-User-Id": "user-1"})
        )
    finally:
        store.close()

    assert created.status_code == 201
    assert created.json()["user_id"] is None
    assert anonymous_sessions.json() == [created.json()]
    assert identified_sessions.json() == []


def test_blank_user_header_returns_a_sanitized_client_error(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    try:
        response = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions",
                headers={"X-User-Id": "   "},
                json={"session_id": "session-1"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 400
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }


def test_history_projects_only_safe_user_and_assistant_messages(tmp_path: Path) -> None:
    histories = {
        ("session-1", "user-1"): [
            HumanMessage(content="请总结课程。"),
            AIMessage(content="课程总结。", name="evaluator"),
            AIMessage(
                content="调用工具中",
                tool_calls=[
                    {
                        "name": "search",
                        "args": {"api_key": "secret-key"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="secret tool output", tool_call_id="call-1"),
            SystemMessage(content="[TASK_RESULTS] internal payload"),
        ]
    }
    app, store = _session_app(tmp_path, HistoryGraph(histories))
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "GET",
                "/sessions/session-1/messages",
                headers={"X-User-Id": "user-1"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json() == [
        {"role": "user", "content": "请总结课程。", "agent": None, "created_at": None},
        {
            "role": "assistant",
            "content": "课程总结。",
            "agent": "evaluator",
            "created_at": None,
        },
    ]
    assert "secret-key" not in response.text
    assert "secret tool output" not in response.text
    assert "internal payload" not in response.text


def test_session_routes_publish_pydantic_contracts_in_openapi() -> None:
    openapi = create_app().openapi()
    create_responses = openapi["paths"]["/sessions"]["post"]["responses"]
    history_responses = openapi["paths"]["/sessions/{session_id}/messages"]["get"]["responses"]

    assert (
        create_responses["201"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/Session"
    )
    assert (
        create_responses["400"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
    assert (
        history_responses["400"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
