"""运行事件的数据边界测试。"""

import pytest
from pydantic import ValidationError

from core.events import ErrorCode, EventType, RunError, RunEvent


def test_run_event_allows_session_id_none() -> None:
    event = RunEvent(
        event_type=EventType.AGENT_STARTED,
        sequence=1,
        session_id=None,
        agent="supervisor",
    )

    assert event.session_id is None


def test_run_event_serializes_replayable_process_fields() -> None:
    event = RunEvent(
        event_type=EventType.TOOL_COMPLETED,
        sequence=3,
        session_id="session-1",
        agent="evaluator",
        tool_name="search",
        success=False,
        duration_ms=12.5,
        error_code=ErrorCode.TOOL_EXECUTION_FAILED,
    )

    assert set(event.model_dump()) == {
        "event_type",
        "sequence",
        "session_id",
        "agent",
        "tool_name",
        "tool_call_id",
        "parent_tool_call_id",
        "input_summary",
        "output_summary",
        "content",
        "message_id",
        "success",
        "duration_ms",
        "error_code",
        "plan_step_sequence",
        "degraded",
        # S2-T1：INTENT_DETECTED 事件携带的意图值，默认 None 向后兼容
        "intent",
        # S2-T3：EVALUATION_COMPLETED 事件携带的评价总结论摘要，
        # 默认 None 向后兼容（旧事件与未评价轮次不携带）
        "evaluation_verdict",
        # S4-T3：RETRIEVAL_DECISION 事件携带的检索决策摘要字段，
        # 默认 None 向后兼容（旧事件与未启用自适应检索的轮次不携带）
        "retrieval_rounds",
        "retrieval_threshold_met",
        "retrieval_stopped_reason",
        "retrieval_hit_count",
        "retrieval_top_score",
        "retrieval_needed",
        "retrieval_need_reason",
    }


def test_run_event_allows_bounded_content_but_rejects_raw_argument_payloads() -> None:
    with pytest.raises(ValidationError):
        RunEvent(
            event_type=EventType.TOOL_STARTED,
            sequence=1,
            session_id="session-1",
            content="可回放思考",
            arguments={"api_key": "secret"},
        )


def test_run_error_has_minimal_diagnostic_fields() -> None:
    error = RunError(
        error_code=ErrorCode.MODEL_CALL_FAILED,
        message="模型不可用",
        agent="supervisor",
    )

    assert error.model_dump() == {
        "error_code": ErrorCode.MODEL_CALL_FAILED,
        "message": "模型不可用",
        "agent": "supervisor",
    }
