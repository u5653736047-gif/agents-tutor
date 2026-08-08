"""用户反馈 REST API 测试(D6-T1)。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pytest import MonkeyPatch

from api.app import create_app


async def _post_feedback(
    app: FastAPI,
    body: dict[str, Any],
    *,
    user_id: str | None = None,
) -> Response:
    transport = ASGITransport(app=app)
    headers: dict[str, str] = {} if user_id is None else {"X-User-Id": user_id}
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/feedback", headers=headers, json=body)


def _feedback_store(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """把 API_FEEDBACK_STORE_PATH 指到 tmp_path 并返回存储路径(测试隔离)。"""
    store_path = tmp_path / "feedback.jsonl"
    monkeypatch.setenv("API_FEEDBACK_STORE_PATH", str(store_path))
    return store_path


def test_feedback_records_only_anonymized_fields(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # 用嵌套父目录路径,顺带覆盖「父目录不存在时自动创建」的分支。
    store_path = tmp_path / "feedback" / "feedback.jsonl"
    monkeypatch.setenv("API_FEEDBACK_STORE_PATH", str(store_path))
    response = asyncio.run(
        _post_feedback(
            create_app(),
            {
                "session_id": "session-1",
                "message_id": "message-1",
                "rating": "down",
                "comment": "回答不够详细",
                "error_code": "model_call_failed",
            },
            user_id="user-1",
        )
    )

    assert response.status_code == 200
    assert response.json() == {"received": True}
    lines = store_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["user_id"] == "user-1"
    assert record["session_id"] == "session-1"
    assert record["message_id"] == "message-1"
    assert record["rating"] == "down"
    assert record["comment"] == "回答不够详细"
    assert record["error_code"] == "model_call_failed"
    # 只存 7 个脱敏引用字段,不含消息全文或其他字段。
    assert set(record) == {
        "user_id",
        "session_id",
        "message_id",
        "rating",
        "comment",
        "error_code",
        "received_at",
    }
    # received_at 必须是可解析的 ISO 时间戳。
    datetime.fromisoformat(record["received_at"])


def test_feedback_rows_keep_their_user_scope(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store_path = _feedback_store(tmp_path, monkeypatch)
    app = create_app()
    asyncio.run(
        _post_feedback(app, {"session_id": "session-1", "rating": "up"}, user_id="user-1")
    )
    asyncio.run(
        _post_feedback(app, {"session_id": "session-2", "rating": "down"}, user_id="user-2")
    )

    records = [
        json.loads(line)
        for line in store_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["user_id"] for record in records] == ["user-1", "user-2"]
    assert [record["session_id"] for record in records] == ["session-1", "session-2"]
    assert [record["rating"] for record in records] == ["up", "down"]


def test_feedback_accepts_anonymous_submissions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store_path = _feedback_store(tmp_path, monkeypatch)
    response = asyncio.run(
        _post_feedback(create_app(), {"session_id": "session-1", "rating": "up"})
    )

    assert response.status_code == 200
    record = json.loads(store_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["user_id"] is None
    assert record["message_id"] is None
    assert record["comment"] is None
    assert record["error_code"] is None


def test_feedback_rejects_unknown_rating(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store_path = _feedback_store(tmp_path, monkeypatch)
    response = asyncio.run(
        _post_feedback(
            create_app(),
            {"session_id": "session-1", "rating": "maybe"},
            user_id="user-1",
        )
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {"error_code": "invalid_request", "message": "Request is invalid."}
    }
    assert not store_path.exists()


def test_feedback_rejects_overlong_comment(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    store_path = _feedback_store(tmp_path, monkeypatch)
    response = asyncio.run(
        _post_feedback(
            create_app(),
            {"session_id": "session-1", "rating": "up", "comment": "x" * 501},
            user_id="user-1",
        )
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error_code"] == "invalid_request"
    assert not store_path.exists()


def test_feedback_store_failure_returns_a_stable_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # API_FEEDBACK_STORE_PATH 指向一个已存在目录:open(..., "a") 会抛
    # OSError(Windows 为 PermissionError,POSIX 为 IsADirectoryError),
    # 路由必须返回稳定的 internal_error,而不是底层错误原文。
    monkeypatch.setenv("API_FEEDBACK_STORE_PATH", str(tmp_path))
    response = asyncio.run(
        _post_feedback(
            create_app(),
            {"session_id": "session-1", "rating": "up"},
            user_id="user-1",
        )
    )

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "error_code": "internal_error",
            "message": "The request could not be completed.",
        }
    }


def test_feedback_publishes_contracts_in_openapi() -> None:
    openapi = create_app().openapi()
    schemas = openapi["components"]["schemas"]
    responses = openapi["paths"]["/feedback"]["post"]["responses"]

    assert schemas["FeedbackRating"]["enum"] == ["up", "down"]
    assert (
        responses["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/FeedbackResponse"
    )
    assert (
        responses["500"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
    assert (
        responses["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
