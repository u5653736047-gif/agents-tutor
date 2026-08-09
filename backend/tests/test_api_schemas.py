"""API 契约模型测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from api.schemas import (
    AgentRole,
    ChatResponse,
    Citation,
    ErrorCode,
    HandoffRequest,
    Message,
    MessageRole,
    PendingHandoff,
    RunError,
    RunEvent,
    Session,
    StreamEventType,
    TaskPlan,
    TaskPlanStatus,
    TaskPlanStep,
    TaskResult,
    WorkerAgentRole,
)


def test_contract_models_represent_a_minimal_chat_response() -> None:
    session = Session(
        session_id="session-1",
        user_id=None,
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, tzinfo=UTC),
        archived=False,
    )
    message = Message(
        role=MessageRole.ASSISTANT,
        content="已完成。",
        agent=AgentRole.SUPERVISOR,
    )
    event = RunEvent(
        event_type=StreamEventType.MESSAGE_END,
        sequence=2,
        agent=AgentRole.SUPERVISOR,
        success=True,
    )
    error = RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="模型调用失败",
        agent=AgentRole.SUPERVISOR,
    )
    handoff = PendingHandoff(
        interrupt_id="interrupt-1",
        request=HandoffRequest(
            target_agent=WorkerAgentRole.TEACHING_ASSISTANT,
            task_content="检查课程设计",
            plan_step_sequence=1,
        ),
    )
    task_plan = TaskPlan(
        steps=[
            TaskPlanStep(
                sequence=1,
                description="检查课程设计",
                target_agent=WorkerAgentRole.TEACHING_ASSISTANT,
            ),
            TaskPlanStep(
                sequence=2,
                description="评估学习效果",
                target_agent=WorkerAgentRole.EVALUATOR,
            ),
        ],
        current_step_index=0,
        status=TaskPlanStatus.ACTIVE,
    )
    response = ChatResponse(
        session_id=session.session_id,
        message=message,
        events=[event],
        run_error=error,
        pending_handoff=handoff,
        references=[
            Citation(
                document_id="document-1",
                source="course-notes",
                page=1,
                chunk_id="chunk-1",
            )
        ],
        task_plan=task_plan,
        task_results=[
            TaskResult(
                step_sequence=1,
                target_agent=WorkerAgentRole.TEACHING_ASSISTANT,
                success=True,
                output="课程设计符合要求",
            )
        ],
        current_agent=AgentRole.SUPERVISOR,
    )

    assert session.user_id is None
    assert response.message == message
    assert response.events == [event]
    assert response.pending_handoff == handoff
    assert response.references is not None
    assert response.task_plan == task_plan


def test_chat_response_keeps_future_fields_optional() -> None:
    response = ChatResponse(session_id="session-1")

    assert response.message is None
    assert response.events == []
    assert response.run_error is None
    assert response.pending_handoff is None
    assert response.references is None
    assert response.task_plan is None
    assert response.task_results is None
    assert response.current_agent is None


def test_stream_event_values_match_the_public_protocol() -> None:
    assert {event.value for event in StreamEventType} == {
        "thinking",
        "reasoning",
        "tool_call",
        "tool_result",
        "message_delta",
        "message_end",
        "agent_switch",
        "error",
        "done",
    }
