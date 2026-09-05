"""学情洞察端点测试（赛前可视化增强：错题归因/正确率趋势/路径回显）。

覆盖：
1. 夹具数据断言 SQL 聚合数值精确（错因分布、加权正确率、路径倒序）；
2. 口径守卫：correct 记录不计入错题统计（即使携带 error_tag）；
3. 多日趋势升序与用户隔离；
4. 降级红线：store 未注入 → 空报告 200；空数据 → 空报告 200；
5. 教师视角 student_id 查询与 OpenAPI 可见性。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from api.app import create_app
from core.learning import LearningRecordStore


async def _get_insights(
    app: FastAPI, user_id: str | None = None, student_id: str | None = None
) -> Response:
    transport = ASGITransport(app=app)
    headers = {} if user_id is None else {"X-User-Id": user_id}
    params = {} if student_id is None else {"student_id": student_id}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(
            "/learning/insights/summary", headers=headers, params=params
        )


def _seeded_store(tmp_path: Path) -> LearningRecordStore:
    """预置夹具数据：student-a 有错题归因分布 + 一条路径存档。"""
    store = LearningRecordStore(tmp_path / "learning.db")
    # 梯度下降两错（同因）+ 一部分正确：错因分布 概念不清×2、计算失误×1
    store.append_record(
        "student-a",
        knowledge_point="梯度下降",
        outcome="incorrect",
        kind="grading",
        error_tag="概念不清",
        question_id="q1",
        source_tool_call_id="t1",
    )
    store.append_record(
        "student-a",
        knowledge_point="梯度下降",
        outcome="incorrect",
        kind="answer",
        error_tag="概念不清",
    )
    store.append_record(
        "student-a",
        knowledge_point="梯度下降",
        outcome="partial",
        kind="answer",
        error_tag="计算失误",
    )
    # 正则化：答对——即使带 error_tag 也不得计入错题统计（口径守卫）
    store.append_record(
        "student-a",
        knowledge_point="正则化",
        outcome="correct",
        kind="answer",
        error_tag="审题偏差",
    )
    # 路径存档两条（倒序回显验证）
    store.append_record(
        "student-a",
        knowledge_point="反向传播",
        outcome="partial",
        kind="path_plan",
    )
    store.append_record(
        "student-a",
        knowledge_point="链式法则",
        outcome="partial",
        kind="path_plan",
    )
    # student-b 一条错题（跨用户隔离验证）
    store.append_record(
        "student-b",
        knowledge_point="卷积",
        outcome="incorrect",
        kind="answer",
        error_tag="方法选择",
    )
    return store


def test_insights_store_aggregates_fixture_exactly(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        data = store.insights("student-a")
    finally:
        store.close()

    # 错题总量：两错 + 一部分正确 = 3（correct 不计）
    assert data["total_wrong"] == 3
    assert data["error_tag_counts"] == {"概念不清": 2, "计算失误": 1}
    # 趋势：夹具同日落库 → 单日一条；加权正确率 (1×1 + 1×0.5)/4 = 0.375
    assert len(data["daily_accuracy"]) == 1
    day = data["daily_accuracy"][0]
    assert day["attempts"] == 4
    assert day["accuracy"] == 0.375
    # 路径倒序：后写入的「链式法则」在前
    plans = data["recent_path_plans"]
    assert [p["knowledge_point"] for p in plans] == ["链式法则", "反向传播"]


def test_insights_daily_accuracy_multi_day_ascending(tmp_path: Path) -> None:
    """多日趋势：改写 created_at 制造跨日数据，断言升序与逐日正确率。"""
    store = LearningRecordStore(tmp_path / "learning.db")
    store.append_record(
        "student-x", knowledge_point="感知机", outcome="incorrect", kind="answer"
    )
    store.append_record(
        "student-x", knowledge_point="感知机", outcome="correct", kind="answer"
    )
    # 把第一条记录的时间回拨两天（夹具操控：测试专用直连改时间戳）
    with sqlite3.connect(tmp_path / "learning.db") as raw:
        raw.execute(
            "UPDATE learning_records SET created_at = '2026-08-01T08:00:00+00:00' "
            "WHERE id = (SELECT MIN(id) FROM learning_records)"
        )
    try:
        data = store.insights("student-x")
    finally:
        store.close()

    daily = data["daily_accuracy"]
    assert [point["date"] for point in daily] == [
        "2026-08-01",
        daily[1]["date"],
    ]
    assert daily[0]["attempts"] == 1
    assert daily[0]["accuracy"] == 0.0
    assert daily[1]["attempts"] == 1
    assert daily[1]["accuracy"] == 1.0


def test_insights_isolates_users(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    try:
        data = store.insights("student-b")
    finally:
        store.close()

    assert data["total_wrong"] == 1
    assert data["error_tag_counts"] == {"方法选择": 1}
    assert data["recent_path_plans"] == []


def test_insights_endpoint_returns_fixture_data(tmp_path: Path) -> None:
    app = create_app()
    store = _seeded_store(tmp_path)
    app.state.learning_store = store
    try:
        response = asyncio.run(_get_insights(app, user_id="student-a"))
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "student-a"
    assert body["total_wrong"] == 3
    assert body["error_tag_counts"] == {"概念不清": 2, "计算失误": 1}
    assert len(body["daily_accuracy"]) == 1
    assert [plan["knowledge_point"] for plan in body["recent_path_plans"]] == [
        "链式法则",
        "反向传播",
    ]


def test_insights_student_id_queries_other_student(tmp_path: Path) -> None:
    """教师视角：显式 student_id 查指定学生的洞察数据。"""
    app = create_app()
    store = _seeded_store(tmp_path)
    app.state.learning_store = store
    try:
        response = asyncio.run(
            _get_insights(app, user_id="teacher-1", student_id="student-b")
        )
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "student-b"
    assert body["total_wrong"] == 1
    assert body["error_tag_counts"] == {"方法选择": 1}


def test_insights_degrades_to_empty_report_without_store() -> None:
    """降级红线：store 未注入（未跑 lifespan）→ 空报告 200 而非报错。"""
    app = create_app()

    response = asyncio.run(_get_insights(app, user_id="student-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["total_wrong"] == 0
    assert body["error_tag_counts"] == {}
    assert body["daily_accuracy"] == []
    assert body["recent_path_plans"] == []


def test_insights_empty_data_returns_empty_report(tmp_path: Path) -> None:
    app = create_app()
    store = LearningRecordStore(tmp_path / "learning.db")
    app.state.learning_store = store
    try:
        response = asyncio.run(_get_insights(app, user_id="fresh-user"))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["total_wrong"] == 0


def test_insights_endpoint_visible_in_openapi() -> None:
    app = create_app()
    schema = app.openapi()

    assert "/learning/insights/summary" in schema["paths"]
    assert "LearningInsights" in schema["components"]["schemas"]
    assert "DailyAccuracyPoint" in schema["components"]["schemas"]
    assert "PathPlanRecordDto" in schema["components"]["schemas"]
