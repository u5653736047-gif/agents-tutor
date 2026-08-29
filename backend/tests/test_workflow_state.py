"""固定工作流状态与契约投影的基座测试（lesson-workflow-design M1）。"""

from typing import Any

import pytest
from pydantic import ValidationError

from api.chat import _public_workflow
from api.schemas import (
    ChatResponse,
    SessionProcess,
    WorkflowProgress,
    WorkflowStep,
)
from core.events import ErrorCode, EventType, RunEvent
from core.state import (
    AgentState,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepState,
    WorkflowStepStatus,
)


def _steps(*statuses: WorkflowStepStatus) -> list[WorkflowStepState]:
    ids = ["collect", "draft", "generate"]
    return [
        WorkflowStepState(step_id=step_id, worker_role="teaching_assistant", status=status)
        for step_id, status in zip(ids, statuses, strict=True)
    ]


class TestWorkflowStepState:
    def test_defaults_to_pending_with_zero_attempts(self) -> None:
        step = WorkflowStepState(step_id="collect", worker_role="teaching_assistant")
        assert step.status is WorkflowStepStatus.PENDING
        assert step.attempts == 0
        assert step.summary is None

    def test_rejects_blank_step_id(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowStepState(step_id="   ", worker_role="teaching_assistant")

    def test_rejects_supervisor_as_worker_role(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowStepState(step_id="collect", worker_role="supervisor")


class TestWorkflowState:
    def test_running_workflow_requires_pending_step(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowState(
                workflow_id="lesson_plan",
                status=WorkflowStatus.RUNNING,
                steps=_steps(
                    WorkflowStepStatus.COMPLETED,
                    WorkflowStepStatus.COMPLETED,
                    WorkflowStepStatus.COMPLETED,
                ),
                current_step_index=3,
            )

    def test_completed_workflow_must_consume_every_step(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowState(
                workflow_id="lesson_plan",
                status=WorkflowStatus.COMPLETED,
                steps=_steps(
                    WorkflowStepStatus.COMPLETED,
                    WorkflowStepStatus.COMPLETED,
                    WorkflowStepStatus.PENDING,
                ),
                current_step_index=2,
            )

    def test_completed_workflow_rejects_failed_steps(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowState(
                workflow_id="lesson_plan",
                status=WorkflowStatus.COMPLETED,
                steps=_steps(
                    WorkflowStepStatus.COMPLETED,
                    WorkflowStepStatus.FAILED,
                    WorkflowStepStatus.COMPLETED,
                ),
                current_step_index=3,
            )

    def test_running_workflow_cannot_carry_error_code(self) -> None:
        with pytest.raises(ValidationError):
            WorkflowState(
                workflow_id="lesson_plan",
                status=WorkflowStatus.RUNNING,
                steps=_steps(
                    WorkflowStepStatus.RUNNING,
                    WorkflowStepStatus.PENDING,
                    WorkflowStepStatus.PENDING,
                ),
                error_code=ErrorCode.WORKFLOW_BUDGET_EXCEEDED,
            )

    def test_failed_workflow_freezes_progress_for_audit(self) -> None:
        state = WorkflowState(
            workflow_id="lesson_plan",
            status=WorkflowStatus.FAILED,
            steps=_steps(
                WorkflowStepStatus.COMPLETED,
                WorkflowStepStatus.FAILED,
                WorkflowStepStatus.PENDING,
            ),
            current_step_index=1,
            error_code=ErrorCode.WORKFLOW_BUDGET_EXCEEDED,
        )
        assert state.error_code is ErrorCode.WORKFLOW_BUDGET_EXCEEDED

    def test_artifacts_must_be_relative_posix_without_traversal(self) -> None:
        base: dict[str, Any] = {
            "workflow_id": "lesson_plan",
            "status": WorkflowStatus.COMPLETED,
            "steps": _steps(
                WorkflowStepStatus.COMPLETED,
                WorkflowStepStatus.COMPLETED,
                WorkflowStepStatus.COMPLETED,
            ),
            "current_step_index": 3,
        }
        WorkflowState(**base, artifacts=["教案-反向传播.docx", "a/b.md"])
        for bad in ("/etc/passwd", "a\\b.docx", "../escape.docx", ""):
            with pytest.raises(ValidationError):
                WorkflowState(**base, artifacts=[bad])


class TestAgentStateChannel:
    def test_agent_state_declares_workflow_channel(self) -> None:
        assert "workflow" in AgentState.__annotations__

    def test_new_run_state_resets_workflow_channel(self) -> None:
        from core.graph_builder import CollaborativeAgentGraph

        state = CollaborativeAgentGraph._new_run_state(
            "下一课",
            "session-1",
            "user-1",
            persisted_values={"workflow": WorkflowState(
                workflow_id="lesson_plan",
                status=WorkflowStatus.FAILED,
                steps=_steps(
                    WorkflowStepStatus.COMPLETED,
                    WorkflowStepStatus.FAILED,
                    WorkflowStepStatus.PENDING,
                ),
                error_code=ErrorCode.WORKFLOW_BUDGET_EXCEEDED,
            )},
        )
        assert state["workflow"] is None


class TestWorkflowEvents:
    def test_event_type_family_is_declared(self) -> None:
        expected = {
            "workflow_started",
            "workflow_step_started",
            "workflow_step_completed",
            "workflow_step_retry",
            "workflow_completed",
            "workflow_failed",
            "workflow_input_queued",
        }
        assert expected <= {member.value for member in EventType}

    def test_budget_error_code_is_declared(self) -> None:
        assert ErrorCode.WORKFLOW_BUDGET_EXCEEDED.value == "workflow_budget_exceeded"

    def test_run_event_carries_workflow_fields(self) -> None:
        event = RunEvent(
            event_type=EventType.WORKFLOW_STEP_COMPLETED,
            sequence=5,
            session_id="session-1",
            agent="teaching_assistant",
            success=True,
            workflow_id="lesson_plan",
            workflow_step_id="collect",
            workflow_step_index=1,
        )
        assert event.workflow_step_index == 1
        assert event.auto_approved is None

    def test_run_event_rejects_non_positive_step_index(self) -> None:
        with pytest.raises(ValidationError):
            RunEvent(
                event_type=EventType.WORKFLOW_STEP_STARTED,
                sequence=5,
                session_id="session-1",
                workflow_step_index=0,
            )


class TestPublicWorkflowProjection:
    def test_projects_core_state_without_machine_paths(self) -> None:
        core = WorkflowState(
            workflow_id="lesson_plan",
            status=WorkflowStatus.RUNNING,
            steps=_steps(
                WorkflowStepStatus.COMPLETED,
                WorkflowStepStatus.RUNNING,
                WorkflowStepStatus.PENDING,
            ),
            current_step_index=1,
            artifact_root="D:\\workspace\\.workflow-artifacts\\run-1",
            artifacts=["教案-反向传播.docx"],
        )
        dto = _public_workflow(core)
        assert dto is not None
        dumped = dto.model_dump(mode="json")
        assert dumped["status"] == "running"
        assert dumped["current_step_index"] == 1
        assert dumped["artifacts"] == ["教案-反向传播.docx"]
        assert "artifact_root" not in dumped
        assert "budget_used" not in dumped
        assert all("artifact_root" not in step for step in dumped["steps"])

    def test_non_workflow_input_projects_to_none(self) -> None:
        assert _public_workflow(None) is None
        assert _public_workflow({"workflow_id": "lesson_plan"}) is None


class TestChatContracts:
    def test_chat_response_accepts_workflow_field(self) -> None:
        response = ChatResponse(
            session_id="session-1",
            workflow=WorkflowProgress(
                workflow_id="lesson_plan",
                status=WorkflowStatus.RUNNING,
                steps=[
                    WorkflowStep(
                        step_id="collect",
                        worker_role="teaching_assistant",
                        status=WorkflowStepStatus.RUNNING,
                        attempts=1,
                    )
                ],
                current_step_index=0,
            ),
        )
        assert response.workflow is not None
        assert response.workflow.steps[0].step_id == "collect"

    def test_session_process_accepts_workflow_field(self) -> None:
        process = SessionProcess()
        assert process.workflow is None
