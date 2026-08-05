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
        {
            "role": "user",
            "content": "请总结课程。",
            "agent": None,
            "created_at": None,
            # D7-T3:core 消息无附件元数据,契约字段显式为 null
            "attachments": None,
        },
        {
            "role": "assistant",
            "content": "课程总结。",
            "agent": "evaluator",
            "created_at": None,
            "attachments": None,
        },
    ]
    assert "secret-key" not in response.text
    assert "secret tool output" not in response.text
    assert "internal payload" not in response.text


def test_history_prefers_agent_role_metadata_over_name(tmp_path: Path) -> None:
    # 构造带角色元数据的历史消息（模拟 a6b31a3 之后 core 写入的 checkpoint）：
    # additional_kwargs["agent"] 是产出角色的稳定来源。这里故意把 name 设成
    # 另一个角色值，验证元数据优先于 name（模型返回的 AIMessage 从不设 name，
    # 但即使设了也不应覆盖元数据）。
    histories = {
        ("session-1", "user-1"): [
            HumanMessage(content="你好"),
            AIMessage(
                content="督导回答",
                additional_kwargs={"agent": "supervisor"},
                name="learning_assistant",
            ),
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
        {"role": "user", "content": "你好", "agent": None, "created_at": None, "attachments": None},
        {
            "role": "assistant",
            "content": "督导回答",
            "agent": "supervisor",
            "created_at": None,
            "attachments": None,
        },
    ]


def test_history_falls_back_to_name_and_degrades_for_legacy_messages(
    tmp_path: Path,
) -> None:
    # 旧数据（a6b31a3 之前）没有角色元数据，覆盖五种回退/降级场景：
    # 1. 无元数据但 name 是合法角色 → 回退读取 name；
    # 2. 无元数据也无 name → 降级为 None（前端显示「助手」徽章）；
    # 3. 元数据值非法（脏数据）→ core 宽容读取返回 None，再回退 name；
    # 4. 元数据非法且 name 也缺失/非法 → 无路可退，降级为 None；
    # 5. 无元数据且 name 是非角色字符串 → 同样降级为 None。
    histories = {
        ("session-1", "user-1"): [
            HumanMessage(content="旧问题"),
            AIMessage(content="旧回答", name="evaluator"),
            AIMessage(content="更旧的回答"),
            AIMessage(
                content="脏数据回答",
                additional_kwargs={"agent": "ghost_role"},
                name="teaching_assistant",
            ),
            AIMessage(
                content="脏数据且无合法回退",
                additional_kwargs={"agent": "ghost_role"},
            ),
            AIMessage(content="name 非法", name="not_a_role"),
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
        {"role": "user", "content": "旧问题", "agent": None, "created_at": None, "attachments": None},
        {
            "role": "assistant",
            "content": "旧回答",
            "agent": "evaluator",
            "created_at": None,
            "attachments": None,
        },
        {
            "role": "assistant",
            "content": "更旧的回答",
            "agent": None,
            "created_at": None,
            "attachments": None,
        },
        {
            "role": "assistant",
            "content": "脏数据回答",
            "agent": "teaching_assistant",
            "created_at": None,
            "attachments": None,
        },
        {
            "role": "assistant",
            "content": "脏数据且无合法回退",
            "agent": None,
            "created_at": None,
            "attachments": None,
        },
        {
            "role": "assistant",
            "content": "name 非法",
            "agent": None,
            "created_at": None,
            "attachments": None,
        },
    ]


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
