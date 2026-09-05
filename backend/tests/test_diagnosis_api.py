"""学情诊断端点测试（六大功能计划 P3-15 验收）。

覆盖：
1. 夹具数据断言 SQL 聚合数值精确（attempts/正确率/预警规则）；
2. 降级红线：store 未注入 → 空报告 200；空数据 → 空报告 200；
3. student_id 教师视角查询（pi 审查 🟡7）：查指定学生数据，且与
   工具层 scope 隔离互不影响（端点是 REST 层入参，不改对话内工具
   的 scope 语义）；
4. OpenAPI 可见性（契约登记）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient, Response

from api.app import create_app
from core.learning import LearningRecordStore


async def _get_summary(
    app, user_id: str | None = None, student_id: str | None = None
) -> Response:
    transport = ASGITransport(app=app)
    headers = {} if user_id is None else {"X-User-Id": user_id}
    params = {} if student_id is None else {"student_id": student_id}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(
            "/learning/diagnosis/summary", headers=headers, params=params
        )


def _seeded_store(tmp_path: Path) -> LearningRecordStore:
    """预置夹具数据：student-a 有一个预警知识点 + 一个健康知识点。"""
    store = LearningRecordStore(tmp_path / "learning.db")
    # 梯度下降：2 错 1 对 → 正确率 1/3 < 0.6 且 attempts≥2 → 预警
    store.append_record(
        "student-a", knowledge_point="梯度下降", outcome="incorrect", kind="grading"
    )
    store.append_record(
        "student-a", knowledge_point="梯度下降", outcome="incorrect", kind="answer"
    )
    store.append_record(
        "student-a", knowledge_point="梯度下降", outcome="correct", kind="answer"
    )
    # 正则化：2 对 → 正确率 1.0，不预警
    store.append_record(
        "student-a", knowledge_point="正则化", outcome="correct", kind="grading"
    )
    store.append_record(
        "student-a", knowledge_point="正则化", outcome="correct", kind="answer"
    )
    # student-b 一条记录（跨用户隔离验证）
    store.append_record(
        "student-b", knowledge_point="卷积", outcome="correct", kind="answer"
    )
    return store


def test_diagnosis_summary_aggregates_fixture_data_exactly(tmp_path: Path) -> None:
    app = create_app()
    store = _seeded_store(tmp_path)
    app.state.learning_store = store
    try:
        response = asyncio.run(_get_summary(app, user_id="student-a"))
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "student-a"
    assert body["total_attempts"] == 5
    assert body["weak_points"] == ["梯度下降"]
    by_point = {p["knowledge_point"]: p for p in body["knowledge_points"]}
    assert by_point["梯度下降"]["attempts"] == 3
    assert by_point["梯度下降"]["correct"] == 1
    assert by_point["梯度下降"]["accuracy"] == round(1 / 3, 3)
    assert by_point["正则化"]["attempts"] == 2
    assert by_point["正则化"]["accuracy"] == 1.0


def test_diagnosis_student_id_queries_other_student(tmp_path: Path) -> None:
    """pi 审查 🟡7：教师视角 student_id 参数查指定学生。"""
    app = create_app()
    store = _seeded_store(tmp_path)
    app.state.learning_store = store
    try:
        # 教师（teacher-1）查 student-b 的学情
        response = asyncio.run(
            _get_summary(app, user_id="teacher-1", student_id="student-b")
        )
    finally:
        store.close()

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == "student-b"
    assert body["total_attempts"] == 1
    assert [p["knowledge_point"] for p in body["knowledge_points"]] == ["卷积"]


def test_diagnosis_without_student_id_uses_current_user(tmp_path: Path) -> None:
    app = create_app()
    store = _seeded_store(tmp_path)
    app.state.learning_store = store
    try:
        response = asyncio.run(_get_summary(app, user_id="student-b"))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["user_id"] == "student-b"
    assert response.json()["total_attempts"] == 1


def test_diagnosis_degrades_to_empty_report_without_store() -> None:
    """降级红线：store 未注入（未跑 lifespan）→ 空报告 200 而非报错。"""
    app = create_app()

    response = asyncio.run(_get_summary(app, user_id="student-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["total_attempts"] == 0
    assert body["knowledge_points"] == []
    assert body["weak_points"] == []


def test_diagnosis_empty_data_returns_empty_report(tmp_path: Path) -> None:
    app = create_app()
    store = LearningRecordStore(tmp_path / "learning.db")
    app.state.learning_store = store
    try:
        response = asyncio.run(_get_summary(app, user_id="fresh-user"))
    finally:
        store.close()

    assert response.status_code == 200
    assert response.json()["total_attempts"] == 0


def test_diagnosis_endpoint_visible_in_openapi() -> None:
    app = create_app()
    schema = app.openapi()

    assert "/learning/diagnosis/summary" in schema["paths"]
    assert "DiagnosisSummary" in schema["components"]["schemas"]
    assert "DiagnosisKnowledgePoint" in schema["components"]["schemas"]
