"""安全、精简的运行事件模型。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    """运行时可观察事件。"""

    # Agent 节点开始执行（agent=角色名）
    AGENT_STARTED = "agent_started"
    # Agent 节点正常结束（agent=角色名）
    AGENT_COMPLETED = "agent_completed"
    # 工具开始执行（tool_name=工具名）
    TOOL_STARTED = "tool_started"
    # 工具执行结束（tool_name=工具名，success 标记成败）
    TOOL_COMPLETED = "tool_completed"
    # 控制权从当前 Agent 切换到下一个
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
    # S4-T3 检索决策：search_knowledge 工具的检索元数据由 core 侧
    # （graph_builder._wrap）解析后发出（agent=调用检索工具的角色，
    # tool_name="search_knowledge"，字段见 RunEvent 的 retrieval_* 注释）。
    # 为什么在 core 侧转换而不是让 knowledge 包直接 emit：knowledge 包
    # 刻意零依赖 core/events.py（见 retrieval.py 模块注释第 8 节第 4 点），
    # 检索层只把决策汇总成 RetrievalMetadata 随结果返回，由本事件承载
    # 决策摘要供评价 Agent 与审计链路核对——「知识库未覆盖」这类结论
    # 不应只在工具输出里一闪而过，事件通道随 checkpoint 持久化。
    RETRIEVAL_DECISION = "retrieval_decision"
    # 计划步骤结果已归档（写入 task_results 通道）
    TASK_RESULT_ARCHIVED = "task_result_archived"
    # 任务计划结果聚合动作发生（成功或失败都会发出）
    TASK_RESULTS_AGGREGATED = "task_results_aggregated"
    # 整个 run 正常结束
    RUN_COMPLETED = "run_completed"
    # 整个 run 以失败结束（配合 state["run_error"]）
    RUN_FAILED = "run_failed"


class ErrorCode(StrEnum):
    """稳定的运行时错误分类。"""

    # 工具层错误
    TOOL_UNKNOWN = "tool_unknown"
    TOOL_UNAUTHORIZED = "tool_unauthorized"
    TOOL_INVALID_ARGUMENTS = "tool_invalid_arguments"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    TOOL_TIMEOUT = "tool_timeout"
    # 模型调用与 ReAct 循环错误
    MODEL_CALL_FAILED = "model_call_failed"
    REACT_ITERATION_LIMIT = "react_iteration_limit"
    # 图编排层错误（切换/分派/聚合等流程失控）
    GRAPH_HANDOFF_LIMIT = "graph_handoff_limit"
    GRAPH_SWITCH_LIMIT = "graph_switch_limit"
    GRAPH_INVALID_TARGET = "graph_invalid_target"
    GRAPH_AGGREGATION_INVALID = "graph_aggregation_invalid"
    # 模型输出不符合 Agent 的 schema 校验
    AGENT_OUTPUT_INVALID = "agent_output_invalid"


class RunEvent(BaseModel):
    """不携带内容、参数或密钥的运行事件。

    安全设计：事件只记录「发生了什么」的轻量事实——类型、角色、工具名、
    耗时、错误码等结构化摘要；消息正文、工具调用参数与密钥都不进事件，
    完整内容留在 state 与 checkpoint 里按需读取，事件流因此可以安全地
    回放、审计和透传给前端。
    """

    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    sequence: int = Field(ge=0)  # 运行内递增序号，保证事件流有序
    session_id: str | None
    agent: str | None = None  # 相关 Agent 角色名
    tool_name: str | None = None  # 相关工具名
    success: bool | None = None  # 成功/失败标记（工具与步骤事件）
    duration_ms: float | None = Field(default=None, ge=0)  # 执行耗时（毫秒）
    error_code: ErrorCode | None = None  # 失败时的错误分类
    plan_step_sequence: int | None = Field(default=None, ge=1)  # 关联的计划步骤序号
    degraded: bool | None = None  # 降级标记（结果缺失回退等场景）
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
    # ── S4-T3 检索决策字段（RETRIEVAL_DECISION 事件携带）──
    # 来源：graph_builder._wrap 从 search_knowledge 成功 ToolResult 的
    # JSON metadata 解析（转换在 core 侧，knowledge 包零依赖本模块）。
    # 脱敏原则：事件只记「决策摘要」，不记查询正文——每轮查询文本已在
    # 工具调用参数与 tool_results 审计中，事件再记一遍会造成双重存储
    # 且可能泄露用户问题细节（need_reason 是策略给出的判定理由，属
    # 决策摘要的一部分，原样携带以便审计者核对「为何不检索」）。
    # 全部默认 None 向后兼容：旧事件、未启用自适应检索的轮次不携带。
    retrieval_rounds: int | None = Field(default=None, ge=0)
    retrieval_threshold_met: bool | None = None
    retrieval_stopped_reason: str | None = None
    retrieval_hit_count: int | None = Field(default=None, ge=0)
    retrieval_top_score: float | None = Field(default=None, ge=0)
    retrieval_needed: bool | None = None
    retrieval_need_reason: str | None = None


class RunError(BaseModel):
    """失败事件使用的最小诊断信息。"""

    model_config = ConfigDict(extra="forbid")

    error_code: ErrorCode
    message: str  # 简短错误摘要（不含敏感正文）
    agent: str | None = None  # 出错的 Agent 角色名


__all__ = ["ErrorCode", "EventType", "RunError", "RunEvent"]
