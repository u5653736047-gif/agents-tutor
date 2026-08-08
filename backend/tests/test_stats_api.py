"""学习进度基础统计 REST API 测试(D6-T7)。

测试策略与 test_session_api 一致:create_app() + app.state 注入替身
(不跑 lifespan,避免真实模型/知识库装配),graph 用最小替身提供
固定历史,断言聚合结果与降级行为。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from api.app import create_app
from core.sessions import SessionStore


class HistoryGraph:
    """提供会话历史的最小 Graph 替身(与 test_session_api 同构)。"""

    def __init__(self, histories: Mapping[tuple[str, str | None], list[BaseMessage]]) -> None:
        self._histories = histories

    def get_history(self, session_id: str, user_id: str | None = None) -> list[BaseMessage]:
        return self._histories.get((session_id, user_id), [])


class RaisingGraph:
    """get_history 必抛异常的替身,验证聚合失败收敛为稳定 500。"""

    def get_history(self, session_id: str, user_id: str | None = None) -> list[BaseMessage]:
        raise RuntimeError("checkpoint read failed")


def _stats_app(tmp_path: Path, graph: HistoryGraph | None = None) -> tuple[FastAPI, SessionStore]:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    if graph is not None:
        app.state.graph = graph
    # graph 不设置时保持 app.state 无该属性,验证「无 graph 装配」降级路径
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


def test_stats_overview_is_empty_on_a_fresh_store(tmp_path: Path) -> None:
    app, store = _stats_app(tmp_path, HistoryGraph({}))
    try:
        response = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json() == {
        "session_count": 0,
        "message_count": 0,
        "agent_answer_counts": {},
        "last_activity_at": None,
    }


def test_stats_overview_aggregates_sessions_messages_and_agent_answers(
    tmp_path: Path,
) -> None:
    # 会话 1:用户消息 + 两条角色元数据回答(其中一条带 tool_calls 的
    # 中间输出不算回答)+ 一条工具消息;会话 2:旧数据(name 回退)。
    histories = {
        ("session-1", "user-1"): [
            HumanMessage(content="问题一"),
            AIMessage(
                content="督导回答",
                additional_kwargs={"agent": "supervisor"},
            ),
            AIMessage(
                content="助学回答",
                additional_kwargs={"agent": "learning_assistant"},
            ),
            AIMessage(
                content="调用工具中",
                tool_calls=[
                    {
                        "name": "search",
                        "args": {"query": "x"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
                additional_kwargs={"agent": "learning_assistant"},
            ),
            ToolMessage(content="tool output", tool_call_id="call-1"),
        ],
        ("session-2", "user-1"): [
            AIMessage(content="助教回答", name="teaching_assistant"),
            AIMessage(content="无法识别角色的回答", name="not_a_role"),
        ],
    }
    app, store = _stats_app(tmp_path, HistoryGraph(histories))
    store.create_session("session-1", user_id="user-1")
    store.create_session("session-2", user_id="user-1")
    try:
        response = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
        # 最近活动:消息级时间不可用,取会话 created_at 的最大值(会话 2
        # 较新)——查询必须在 close 之前(close 后 sqlite 连接已关闭)。
        records = store.list_sessions(user_id="user-1")
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["session_count"] == 2
    # 全量消息计数:会话 1 的 5 条 + 会话 2 的 2 条(含工具/中间消息)
    assert body["message_count"] == 7
    # 回答分布:_safe_agent 口径;带 tool_calls 的中间输出与无法识别
    # 角色的回答均不计入任何角色
    assert body["agent_answer_counts"] == {
        "supervisor": 1,
        "learning_assistant": 1,
        "teaching_assistant": 1,
    }
    assert body["last_activity_at"] == max(
        record.created_at for record in records
    ).isoformat()


def test_stats_overview_only_counts_the_current_user(tmp_path: Path) -> None:
    # 两个用户各自的会话历史相互隔离:user-1 只看到自己的 1 个会话,
    # user-2 只看到自己的 2 个会话,越权会话不计入。
    histories = {
        ("session-a", "user-1"): [
            HumanMessage(content="user-1 的问题"),
            AIMessage(
                content="user-1 的回答",
                additional_kwargs={"agent": "supervisor"},
            ),
        ],
        ("session-b", "user-2"): [
            HumanMessage(content="user-2 的问题一"),
            AIMessage(
                content="user-2 的回答一",
                additional_kwargs={"agent": "learning_assistant"},
            ),
            HumanMessage(content="user-2 的问题二"),
            AIMessage(
                content="user-2 的回答二",
                additional_kwargs={"agent": "evaluator"},
            ),
        ],
        ("session-c", "user-2"): [
            HumanMessage(content="user-2 的问题三"),
        ],
    }
    app, store = _stats_app(tmp_path, HistoryGraph(histories))
    store.create_session("session-a", user_id="user-1")
    store.create_session("session-b", user_id="user-2")
    store.create_session("session-c", user_id="user-2")
    try:
        first_user = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
        second_user = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-2"})
        )
    finally:
        store.close()

    assert first_user.status_code == 200
    assert first_user.json()["session_count"] == 1
    assert first_user.json()["message_count"] == 2
    assert first_user.json()["agent_answer_counts"] == {"supervisor": 1}

    assert second_user.status_code == 200
    assert second_user.json()["session_count"] == 2
    assert second_user.json()["message_count"] == 5
    assert second_user.json()["agent_answer_counts"] == {
        "learning_assistant": 1,
        "evaluator": 1,
    }


def test_stats_overview_includes_archived_sessions(tmp_path: Path) -> None:
    # 进度是历史累计:归档会话仍计入(避免用户归档后进度「清零」)。
    histories = {
        ("session-1", "user-1"): [
            HumanMessage(content="已完成的问题"),
            AIMessage(
                content="已完成的回答",
                additional_kwargs={"agent": "supervisor"},
            ),
        ],
    }
    app, store = _stats_app(tmp_path, HistoryGraph(histories))
    store.create_session("session-1", user_id="user-1")
    store.archive_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["session_count"] == 1
    assert response.json()["message_count"] == 2


def test_stats_overview_degrades_when_graph_is_missing(tmp_path: Path) -> None:
    # 无 graph 装配(测试直接 create_app() 且未注入):会话数正常统计,
    # 消息相关字段降级为零/空,last_activity_at 回退会话 created_at。
    app, store = _stats_app(tmp_path)
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
        # close 前取会话 created_at(close 后 sqlite 连接已关闭)
        created_at = store.list_sessions(user_id="user-1")[0].created_at.isoformat()
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json() == {
        "session_count": 1,
        "message_count": 0,
        "agent_answer_counts": {},
        "last_activity_at": created_at,
    }


def test_stats_overview_without_user_header_uses_anonymous_scope(tmp_path: Path) -> None:
    # 与 sessions 路由的匿名先例一致:无 X-User-Id 统计匿名命名空间,
    # 不返回 422,也不混入具名用户的会话。
    histories = {
        ("anon-session", None): [
            HumanMessage(content="匿名问题"),
            AIMessage(content="匿名回答", additional_kwargs={"agent": "supervisor"}),
        ],
    }
    app, store = _stats_app(tmp_path, HistoryGraph(histories))
    store.create_session("anon-session", user_id=None)
    store.create_session("named-session", user_id="user-1")
    try:
        anonymous = asyncio.run(_request(app, "GET", "/stats/overview"))
        named = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
    finally:
        store.close()

    assert anonymous.status_code == 200
    assert anonymous.json()["session_count"] == 1
    assert anonymous.json()["message_count"] == 2
    assert anonymous.json()["agent_answer_counts"] == {"supervisor": 1}
    assert named.json()["session_count"] == 1
    assert named.json()["message_count"] == 0


def test_stats_overview_converges_aggregation_failures_to_500(tmp_path: Path) -> None:
    app = create_app()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    app.state.session_store = store
    app.state.graph = RaisingGraph()
    store.create_session("session-1", user_id="user-1")
    try:
        response = asyncio.run(
            _request(app, "GET", "/stats/overview", headers={"X-User-Id": "user-1"})
        )
    finally:
        store.close()

    assert response.status_code == 500
    assert response.json()["detail"]["error_code"] == "internal_error"


def test_stats_overview_publishes_contract_in_openapi() -> None:
    openapi = create_app().openapi()
    overview = openapi["paths"]["/stats/overview"]["get"]

    assert (
        overview["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/StatsOverview"
    )
    assert (
        overview["responses"]["500"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
    stats_schema = openapi["components"]["schemas"]["StatsOverview"]
    assert set(stats_schema["properties"]) == {
        "session_count",
        "message_count",
        "agent_answer_counts",
        "last_activity_at",
    }
