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
from core.events import EventType, RunEvent
from core.sessions import SessionStore


class HistoryGraph:
    """提供会话历史的最小 Graph 替身。"""

    def __init__(
        self,
        histories: Mapping[tuple[str, str | None], list[BaseMessage]],
        states: Mapping[tuple[str, str | None], dict[str, Any]] | None = None,
    ) -> None:
        self._histories = histories
        self._states = states or {}

    def get_history(self, session_id: str, user_id: str | None = None) -> list[BaseMessage]:
        return self._histories.get((session_id, user_id), [])

    def get_state(
        self, session_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        return self._states.get((session_id, user_id))


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


def test_session_api_selects_and_persists_an_absolute_workspace(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    workspace = tmp_path / "selected-project"
    workspace.mkdir()
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions",
                headers=headers,
                json={
                    "session_id": "session-workspace",
                    "workspace_root": str(workspace),
                },
            )
        )
        listed = asyncio.run(_request(app, "GET", "/sessions", headers=headers))
    finally:
        store.close()

    assert created.status_code == 201
    assert created.json()["workspace_root"] == str(workspace.resolve())
    assert created.json()["additional_workspace_roots"] == []
    assert created.json()["workspace_access"] == "read_only"
    assert listed.json()[0]["workspace_root"] == str(workspace.resolve())


def test_session_api_adds_an_authorized_workspace_directory(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    primary = tmp_path / "primary"
    shared = tmp_path / "shared"
    primary.mkdir()
    shared.mkdir()
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions",
                headers=headers,
                json={"session_id": "session-1", "workspace_root": str(primary)},
            )
        )
        updated = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions/session-1/workspace-roots",
                headers=headers,
                json={"path": str(shared)},
            )
        )
    finally:
        store.close()

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["workspace_root"] == str(primary.resolve())
    assert updated.json()["additional_workspace_roots"] == [str(shared.resolve())]


def test_workspace_api_validates_and_browses_server_directories(tmp_path: Path) -> None:
    app, store = _session_app(tmp_path)
    workspace = tmp_path / "workspace"
    child = workspace / "child"
    child.mkdir(parents=True)
    try:
        validated = asyncio.run(
            _request(
                app,
                "POST",
                "/workspaces/validate",
                json={"path": str(workspace)},
            )
        )
        browsed = asyncio.run(
            _request(
                app,
                "GET",
                f"/workspaces/directories?path={workspace}",
            )
        )
    finally:
        store.close()

    assert validated.status_code == 200
    assert validated.json() == {
        "path": str(workspace.resolve()),
        "name": "workspace",
    }
    assert browsed.status_code == 200
    assert browsed.json()["path"] == str(workspace.resolve())
    assert browsed.json()["directories"] == [
        {"name": "child", "path": str(child.resolve())}
    ]


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
        denied_process = asyncio.run(
            _request(app, "GET", "/sessions/private/process", headers=second_user)
        )
    finally:
        store.close()

    assert first_created.status_code == 201
    assert second_created.status_code == 201
    assert denied_archive.status_code == 404
    assert denied_history.status_code == 404
    assert denied_process.status_code == 404
    assert denied_archive.json()["detail"]["error_code"] == "session_not_found"
    assert denied_history.json()["detail"]["error_code"] == "session_not_found"
    assert denied_process.json()["detail"]["error_code"] == "session_not_found"


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
            # P2-12:无批改元数据时显式为 null
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "课程总结。",
            "agent": "evaluator",
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
    ]
    assert "secret-key" not in response.text
    assert "secret tool output" not in response.text
    assert "internal payload" not in response.text


def test_history_restores_message_level_grading_metadata(
    tmp_path: Path,
) -> None:
    """审查 W3：任意历史轮的批改卡经 history 端点恢复（pi 审查 🟡4
    修复的最后一公里守护：若 _public_message 的 grading 映射被未来
    重构破坏，本用例直接失败）。"""
    from core.state import GradingItem, GradingResult, with_grading

    graded_message = with_grading(
        AIMessage(content="批改完成。", name="evaluator"),
        GradingResult(
            items=[
                GradingItem(
                    question_id="q1",
                    score=8,
                    max_score=10,
                    feedback="基本正确。",
                    knowledge_point="梯度下降",
                )
            ],
            overall_comment="总体良好。",
            total_score=8,
            max_total_score=10,
        ),
    )
    histories = {
        ("session-graded", "user-1"): [
            HumanMessage(content="请批改我的作业"),
            graded_message,
            AIMessage(content="已为你批改本次作业。", name="supervisor"),
        ]
    }
    app, store = _session_app(tmp_path, HistoryGraph(histories))
    store.create_session("session-graded", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "GET",
                "/sessions/session-graded/messages",
                headers={"X-User-Id": "user-1"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    messages = response.json()
    # 批改作答消息：grading 元数据完整透出
    graded = messages[1]
    assert graded["grading"] is not None
    assert graded["grading"]["total_score"] == 8
    assert graded["grading"]["max_total_score"] == 10
    assert graded["grading"]["items"][0]["knowledge_point"] == "梯度下降"
    assert graded["grading"]["overall_comment"] == "总体良好。"
    # 非批改消息保持 null（用户消息与聚合回答）
    assert messages[0]["grading"] is None
    assert messages[2]["grading"] is None


def test_process_history_replays_reasoning_and_redacted_tool_details(
    tmp_path: Path,
) -> None:
    events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=1,
            session_id="session-1",
            agent="learning_assistant",
        ),
        RunEvent(
            event_type=EventType.AGENT_REASONING,
            sequence=2,
            session_id="session-1",
            agent="learning_assistant",
            content="先检索定义，再用例子解释",
            message_id="assistant-step-1",
        ),
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=3,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            tool_call_id="call-search-1",
            input_summary='{"query":"反向传播","api_key":"[REDACTED]"}',
        ),
        RunEvent(
            event_type=EventType.TOOL_COMPLETED,
            sequence=4,
            session_id="session-1",
            agent="learning_assistant",
            tool_name="search_knowledge",
            tool_call_id="call-search-1",
            success=True,
            output_summary='{"found":true,"hits":2}',
        ),
    ]
    graph = HistoryGraph(
        {},
        {
            ("session-1", "user-1"): {
                "events": events,
                "current_agent": "learning_assistant",
                "task_plan": None,
                "task_results": [],
            }
        },
    )
    app, store = _session_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "GET",
                "/sessions/session-1/process",
                headers={"X-User-Id": "user-1"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_agent"] == "learning_assistant"
    assert [event["event_type"] for event in payload["events"]] == [
        "thinking",
        "reasoning",
        "tool_call",
        "tool_result",
    ]
    assert payload["events"][1]["content"] == "先检索定义，再用例子解释"
    assert payload["events"][2]["input_summary"] == (
        '{"query":"反向传播","api_key":"[REDACTED]"}'
    )
    assert payload["events"][3]["output_summary"] == '{"found":true,"hits":2}'
    assert "sk-live-secret" not in response.text


def test_process_history_returns_only_the_latest_tagged_run(tmp_path: Path) -> None:
    events = [
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=1,
            session_id="session-1",
            run_id="run-1",
            agent="supervisor",
        ),
        RunEvent(
            event_type=EventType.RUN_COMPLETED,
            sequence=2,
            session_id="session-1",
            run_id="run-1",
        ),
        RunEvent(
            event_type=EventType.AGENT_STARTED,
            sequence=3,
            session_id="session-1",
            run_id="run-2",
            agent="learning_assistant",
        ),
        RunEvent(
            event_type=EventType.AGENT_REASONING,
            sequence=4,
            session_id="session-1",
            run_id="run-2",
            agent="learning_assistant",
            content="只展示本轮",
        ),
    ]
    graph = HistoryGraph(
        {},
        {
            ("session-1", "user-1"): {
                "run_id": "run-2",
                "events": events,
                "current_agent": "learning_assistant",
                "task_plan": None,
                "task_results": [],
            }
        },
    )
    app, store = _session_app(tmp_path, graph)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(
                app,
                "GET",
                "/sessions/session-1/process",
                headers={"X-User-Id": "user-1"},
            )
        )
    finally:
        store.close()

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-2"
    assert [event["sequence"] for event in payload["events"]] == [3, 4]
    assert {event["run_id"] for event in payload["events"]} == {"run-2"}


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
        {
            "role": "user",
            "content": "你好",
            "agent": None,
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "督导回答",
            "agent": "supervisor",
            "created_at": None,
            "attachments": None,
            "grading": None,
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
        {
            "role": "user",
            "content": "旧问题",
            "agent": None,
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "旧回答",
            "agent": "evaluator",
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "更旧的回答",
            "agent": None,
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "脏数据回答",
            "agent": "teaching_assistant",
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "脏数据且无合法回退",
            "agent": None,
            "created_at": None,
            "attachments": None,
            "grading": None,
        },
        {
            "role": "assistant",
            "content": "name 非法",
            "agent": None,
            "created_at": None,
            "attachments": None,
            "grading": None,
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


def test_session_response_carries_nullable_title(tmp_path: Path) -> None:
    """UX-20260808#1:会话契约携带 title——新建为 None,写入后随列表返回。"""
    app, store = _session_app(tmp_path)
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(app, "POST", "/sessions", headers=headers, json={"session_id": "session-1"})
        )
        store.set_title_if_absent("session-1", "什么是注意力机制", user_id="user-1")
        listed = asyncio.run(_request(app, "GET", "/sessions", headers=headers))
    finally:
        store.close()

    assert created.status_code == 201
    assert "updated_at" in created.json()
    assert created.json()["updated_at"] == created.json()["created_at"]
    assert created.json()["title"] is None
    assert [session["title"] for session in listed.json()] == ["什么是注意力机制"]


# ── S5-C1 知识空间绑定 ─────────────────────────────────────────────


def test_session_create_round_trips_knowledge_namespace(tmp_path: Path) -> None:
    """创建会话携带 knowledge_namespace → 响应、清单与存储往返该值。"""
    app, store = _session_app(tmp_path)
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions",
                headers=headers,
                json={"session_id": "s-ns", "knowledge_namespace": "course-a"},
            )
        )
        listed = asyncio.run(_request(app, "GET", "/sessions", headers=headers))
        record = store.get_session("s-ns", user_id="user-1")
    finally:
        store.close()

    assert created.status_code == 201
    assert created.json()["knowledge_namespace"] == "course-a"
    assert listed.status_code == 200
    assert listed.json()[0]["knowledge_namespace"] == "course-a"
    assert record is not None
    assert record.knowledge_namespace == "course-a"


def test_session_create_rejects_invalid_knowledge_namespace(tmp_path: Path) -> None:
    """非法空间标识（大写/空格/连字符边界）→ 422 invalid_request。

    规则对齐 manifest 的 source 标识（ingest_books._SOURCE_PATTERN）：
    小写字母开头，只含小写字母/数字，连字符只能单根内嵌——首尾或
    连续连字符均拒绝。
    """
    app, store = _session_app(tmp_path)
    headers = {"X-User-Id": "user-1"}
    try:
        for bad_namespace in ("Course A", "course-", "-course", "a--b"):
            response = asyncio.run(
                _request(
                    app,
                    "POST",
                    "/sessions",
                    headers=headers,
                    json={"session_id": "s-bad", "knowledge_namespace": bad_namespace},
                )
            )
            assert response.status_code == 422, bad_namespace
            body = response.json()
            assert body["detail"]["error_code"] == "invalid_request", bad_namespace
    finally:
        store.close()


def test_session_create_accepts_single_inner_hyphen_namespace(
    tmp_path: Path,
) -> None:
    """合法形态：单根内嵌连字符（course-a）通过；与 manifest 规则一致。"""
    app, store = _session_app(tmp_path)
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(
                app,
                "POST",
                "/sessions",
                headers=headers,
                json={"session_id": "s-hyphen", "knowledge_namespace": "course-a"},
            )
        )
        record = store.get_session("s-hyphen", user_id="user-1")
    finally:
        store.close()

    assert created.status_code == 201
    assert created.json()["knowledge_namespace"] == "course-a"
    assert record is not None


def test_session_create_defaults_to_unbound_namespace(tmp_path: Path) -> None:
    """缺省创建 → knowledge_namespace 为 None（未绑定，检索走单路 public）。"""
    app, store = _session_app(tmp_path)
    headers = {"X-User-Id": "user-1"}
    try:
        created = asyncio.run(
            _request(app, "POST", "/sessions", headers=headers, json={"session_id": "s-def"})
        )
        record = store.get_session("s-def", user_id="user-1")
    finally:
        store.close()

    assert created.status_code == 201
    assert created.json()["knowledge_namespace"] is None
    assert record is not None
    assert record.knowledge_namespace is None
