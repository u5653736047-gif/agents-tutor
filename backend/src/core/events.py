"""安全、精简的运行事件模型。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """运行时可观察事件。"""

    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    AGENT_SWITCHED = "agent_switched"
    # S2-T1 意图识别：Supervisor 完成本轮意图分类后发出（agent=supervisor，
    # intent=意图枚举值字符串）。事件是瞬时的运行时信号，权威值以 state["intent"]
    # 为准（checkpoint 持久化）；消费方（api/chat.py 的 EVENT_TYPE_MAP 白名单）
    # 对未映射的新事件类型安全跳过，因此新增类型不会破坏既有事件协议。
    INTENT_DETECTED = "intent_detected"
    # S2-T3 评价：评价 Agent 完成一轮结构化评价后发出（agent=evaluator，
    # evaluation_verdict=总结论枚举值字符串）。脱敏原则：事件只记录结论
    # 摘要（verdict 枚举值，无敏感正文），理由等完整内容存 state["evaluation"]
    # （随 checkpoint 持久化，供审计读取）——与「事件不记录敏感正文」的
    # 仓库惯例一致（RunEvent 无 content/arguments 字段，见该类注释）。
    EVALUATION_COMPLETED = "evaluation_completed"
    TASK_RESULT_ARCHIVED = "task_result_archived"
    TASK_RESULTS_AGGREGATED = "task_results_aggregated"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class ErrorCode(StrEnum):
    """稳定的运行时错误分类。"""

    TOOL_UNKNOWN = "tool_unknown"
    TOOL_UNAUTHORIZED = "tool_unauthorized"
    TOOL_INVALID_ARGUMENTS = "tool_invalid_arguments"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_TIMEOUT = "tool_timeout"
    MODEL_CALL_FAILED = "model_call_failed"
    REACT_ITERATION_LIMIT = "react_iteration_limit"
    GRAPH_HANDOFF_LIMIT = "graph_handoff_limit"
    GRAPH_SWITCH_LIMIT = "graph_switch_limit"
    GRAPH_INVALID_TARGET = "graph_invalid_target"
    GRAPH_AGGREGATION_INVALID = "graph_aggregation_invalid"
    AGENT_OUTPUT_INVALID = "agent_output_invalid"


class RunEvent(BaseModel):
    """不携带内容、参数或密钥的运行事件。"""

    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    sequence: int = Field(ge=0)
    session_id: str | None
    agent: str | None = None
    tool_name: str | None = None
    success: bool | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    error_code: ErrorCode | None = None
    plan_step_sequence: int | None = Field(default=None, ge=1)
    degraded: bool | None = None
    # S2-T1：INTENT_DETECTED 事件携带的意图枚举值（如 "lesson_prep"）。
    # 放在事件本体而不是塞进 agent/tool_name，是为了让消费方按字段读取，
    # 与 TOOL_COMPLETED 携带 tool_name 的既有约定保持一致。
    intent: str | None = None
    # S2-T3：EVALUATION_COMPLETED 事件携带的评价总结论枚举值
    # （如 "pass"/"questionable"/"fail"，见 EvaluationVerdict 注释）。
    # 脱敏原则：事件只带结论摘要，不带 reason 理由正文（理由可能包含
    # 被评价内容的细节，属敏感正文；完整结论存 state["evaluation"] 供
    # 审计读取）。默认 None 向后兼容——旧事件与未评价轮次不携带该字段。
    evaluation_verdict: str | None = None


class RunError(BaseModel):
    """失败事件使用的最小诊断信息。"""

    model_config = ConfigDict(extra="forbid")

    error_code: ErrorCode
    message: str
    agent: str | None = None


__all__ = ["ErrorCode", "EventType", "RunError", "RunEvent"]
