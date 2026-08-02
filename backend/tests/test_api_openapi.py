"""OpenAPI contract registration tests."""

from __future__ import annotations

from api.app import create_app


def test_openapi_includes_the_bridge_contract_models() -> None:
    openapi = create_app().openapi()
    schemas = openapi["components"]["schemas"]

    assert {
        "Session",
        "Message",
        "RunEvent",
        "RunError",
        "PendingHandoff",
        "ChatResponse",
        "Citation",
        "TaskPlanStep",
    }.issubset(schemas)
    assert schemas["StreamEventType"]["enum"] == [
        "thinking",
        "tool_call",
        "tool_result",
        "message_end",
        "agent_switch",
        "error",
        "done",
    ]

    chat_response = schemas["ChatResponse"]
    assert "references" in chat_response["properties"]
    assert "task_plan" in chat_response["properties"]
    assert "references" not in chat_response.get("required", [])
    assert "task_plan" not in chat_response.get("required", [])
