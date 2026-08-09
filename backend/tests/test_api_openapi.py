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
        "SessionProcess",
    }.issubset(schemas)
    assert schemas["StreamEventType"]["enum"] == [
        "thinking",
        "reasoning",
            "tool_call",
            "tool_result",
            "tool_output",
            "approval_required",
            "message_delta",
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


def test_chat_validation_error_uses_the_public_error_contract() -> None:
    openapi = create_app().openapi()
    responses = openapi["paths"]["/chat"]["post"]["responses"]

    assert (
        responses["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )


def test_session_validation_errors_use_the_public_error_contract() -> None:
    openapi = create_app().openapi()
    operations = (
        openapi["paths"]["/sessions"]["post"],
        openapi["paths"]["/sessions"]["get"],
        openapi["paths"]["/sessions/{session_id}/archive"]["post"],
        openapi["paths"]["/sessions/{session_id}/messages"]["get"],
    )

    for operation in operations:
        assert (
            operation["responses"]["422"]["content"]["application/json"]["schema"][
                "$ref"
            ]
            == "#/components/schemas/ErrorResponse"
        )


def test_handoff_routes_expose_only_the_minimal_approval_contract() -> None:
    openapi = create_app().openapi()
    schemas = openapi["components"]["schemas"]
    handoff_path = openapi["paths"]["/sessions/{session_id}/handoff"]

    assert (
        handoff_path["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/PendingHandoffResponse"
    )
    assert (
        handoff_path["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/ChatResponse"
    )
    assert (
        handoff_path["post"]["responses"]["409"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/ErrorResponse"
    )
    # D2-T4:审批修改工作流上线,HandoffDecisionAction 增加 modify
    assert schemas["HandoffDecisionAction"]["enum"] == [
        "confirm",
        "reject",
        "modify",
    ]
    assert set(schemas["HandoffDecisionRequest"]["properties"]) == {
        "action",
        "interrupt_id",
        "target_agent",
        "task_content",
    }
