"""基于统一 ReAct Agent 的 LangGraph 编排。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Collection, Hashable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .context import MessageTokenCounter
from .events import ErrorCode, EventType, RunError, RunEvent
from .filesystem import workspace_scope
from .knowledge.models import Citation
from .knowledge.tools import knowledge_scope
from .learning import LEARNING_OUTCOMES, LearningRecordStore, learning_scope
from .nodes import ReActAgentNode, ReActResult, create_agent_nodes
from .nodes.prompts import TOOL_ORCHESTRATION_SUPERVISOR_PROMPT, WORKFLOW_SUPERVISOR_CLAUSE
from .nodes.react_agent import ChatModel, tool_output_summary
from .state import (
    REFERENCES_METADATA_KEY,
    AgentRole,
    AgentState,
    EvaluationResult,
    EvaluationVerdict,
    GeneratedFile,
    GradingItem,
    GradingResult,
    HandoffApprovalAction,
    HandoffApprovalDecision,
    HandoffApprovalRequest,
    Intent,
    PendingHandoffApproval,
    PendingToolApproval,
    ReferenceVerification,
    StudentLevel,
    TaskContext,
    TaskPlan,
    TaskPlanStatus,
    TaskPlanStep,
    TaskStepResult,
    ToolApprovalAction,
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolResult,
    WorkflowState,
    WorkflowStatus,
    WorkflowStepStatus,
    create_initial_state,
    message_references,
    with_agent_role,
    with_generated_files,
    with_grading,
    with_references,
)
from .tools import DEFAULT_TOOL_TIMEOUT_SECONDS, ToolRegistry
from .tools.office_tools import GENERATED_FILES_RESULT_KEY, approved_office_execution
from .tools.shell_tool import approved_shell_execution, shell_output_scope
from .workflows import (
    WorkflowDefinition,
    WorkflowStepDefinition,
    get_workflow,
    registered_workflow_ids,
    sanitize_artifact_filename,
)

WorkerRole = Literal["teaching_assistant", "learning_assistant", "evaluator"]
OrchestrationMode = Literal["handoff", "tool"]
CompiledAgentGraph = CompiledStateGraph[AgentState, None, AgentState, AgentState]
_HANDOFF_APPROVAL_NODE = "handoff_approval"
_TOOL_APPROVAL_NODE = "tool_approval"
_TASK_PLAN_DISPATCH_NODE = "task_plan_dispatch"
# 固定工作流确定性调度节点（lesson-workflow-design §二）：仅 tool 模式
# 且 enable_workflows 时可达；handoff 编译图不注册该路由目标。
_WORKFLOW_DISPATCH_NODE = "workflow_dispatch"
_TASK_RESULTS_MARKER = "[TASK_RESULTS]"
_SUBAGENT_TOOL_NAMES: dict[AgentRole, str] = {
    AgentRole.TEACHING_ASSISTANT: "ask_teaching_assistant",
    AgentRole.LEARNING_ASSISTANT: "ask_learning_assistant",
    AgentRole.EVALUATOR: "ask_evaluator",
}
_SUBAGENT_TOOL_TIMEOUT_SECONDS = 180.0
_ACTIVE_PARENT_STATE: ContextVar[AgentState | None] = ContextVar(
    "active_parent_agent_state",
    default=None,
)
_SUBAGENT_EVENT_TRACES: ContextVar[list[list[RunEvent]] | None] = ContextVar(
    "subagent_event_traces",
    default=None,
)

# ── S2-T4 引用收集：产出结构化引用的检索工具名集合 ──
# 这是「按证据类型过滤检索类工具」的依据：只有这些工具的成功输出才会
# 被解析出 Citation 并挂到最终回答（见 _citations_from_tool_results 注释）。
# 新增检索类工具时在此追加工具名即可，无需改其他代码；读取侧
# （message_references）不依赖此常量，直接读消息元数据。
_CITATION_TOOL_NAMES = frozenset(
    {"search_knowledge", *_SUBAGENT_TOOL_NAMES.values()}
)


class _SubagentTaskInput(BaseModel):
    """Supervisor 交给专业 Agent 的隔离任务描述。"""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=8000)


class _TaskPlanInput(BaseModel):
    """仅暴露给模型的计划输入，不允许模型伪造运行时游标。"""

    model_config = ConfigDict(extra="forbid")

    steps: list[TaskPlanStep] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_complete_plan(self) -> _TaskPlanInput:
        TaskPlan(steps=self.steps)
        return self


@tool
def handoff(target: WorkerRole) -> str:
    """将当前任务交给指定的专业 Agent。"""
    return target


@tool(args_schema=_TaskPlanInput)
def create_task_plan(steps: list[TaskPlanStep]) -> str:
    """为需要至少两个有序子任务的复杂请求创建一次任务计划。"""
    return TaskPlan(steps=steps).model_dump_json()


# ── S5-A1/A2：tool 模式的计划执行管控 ─────────────────────────
# 设计见类与函数注释。核心思路：handoff 模式的顺序管控在图结构里
# （调度节点逐 Worker 分派），tool 模式没有这个结构——Supervisor 在
# 自己的 ReAct 循环内经 ask_* 同步调子代理，顺序控制必须下沉到工具层，
# 不能交给模型自觉。

# 每计划重试预算（A2 有界防循环）：所有步骤共享，用完即按 abort 收口。
_TOOL_PLAN_RETRY_BUDGET = 1

# TaskStepResult 校验器允许的「本地可恢复」失败分类（与 handoff 模式
# Worker 轮能产生的错误码同一集合）；不在集合内的子代理异常统一归一为
# AGENT_OUTPUT_INVALID（保持既有不变量，不扩校验集合）。
_SUBAGENT_STEP_ERROR_CODES = {
    ErrorCode.MODEL_CALL_FAILED,
    ErrorCode.REACT_ITERATION_LIMIT,
    ErrorCode.AGENT_OUTPUT_INVALID,
}


class _SubagentRunError(RuntimeError):
    """子代理同步执行失败，携带可归档进 TaskStepResult 的稳定错误分类。"""

    def __init__(self, error_code: ErrorCode) -> None:
        super().__init__("subagent execution failed")
        self.error_code = error_code


def _archivable_step_error(error_code: ErrorCode) -> ErrorCode:
    """归一到 TaskStepResult 校验器允许的错误分类（见上方集合注释）。"""
    return error_code if error_code in _SUBAGENT_STEP_ERROR_CODES else ErrorCode.AGENT_OUTPUT_INVALID


class _ToolPlanHolder:
    """轮内计划执行状态的可变持有者（跨线程共享）。

    为什么需要一层盒子：工具在线程池中执行（executor 用 copy_context()
    快照传播上下文）——工具内对 ContextVar 的 set 只改快照副本，主线程
    轮末不可见；而 holder 对象本身是引用共享的，工具改 holder.execution
    （如同轮新建计划、逐步记账），_wrap 轮末读同一对象即可拿到全部变化。
    """

    def __init__(self, execution: _ToolModePlanExecution | None) -> None:
        self.execution = execution


_TOOL_PLAN_EXECUTION: ContextVar[_ToolPlanHolder | None] = ContextVar(
    "tool_mode_plan_execution",
    default=None,
)


class _ToolModePlanExecution:
    """一次 Supervisor 轮内 tool 模式计划执行的可变状态。

    为什么需要轮内可变状态：ask_* 可能在同一 ReAct 轮内被多次调用，
    而计划的游标推进要到轮末才写回 state——工具层门控若直接读
    state["task_plan"] 会拿到过期游标，无法拦住「同轮第二次乱序调用」。
    本对象由 _wrap 在 agent.run 前创建并经 ContextVar 注入工具闭包，
    每次成功/失败即时推进；轮末由 _wrap 读回并写入 updates。

    失败记录口径（A2）：只有终局结果落 task_results——continue/abort
    落失败结果，retry 中间失败只计 retries_used 不落结果。这保持
    「每步至多一条结果、连续前缀」的不变量（_validate_task_result_prefix
    依赖），重试痕迹经 retries_used 与工具事件审计可见。
    """

    def __init__(self, plan: TaskPlan, results: list[TaskStepResult]) -> None:
        self.plan = plan
        self.results = list(results)
        self.retries_used = plan.retries_used
        # 本轮起点：结果数超过它的新增部分即本轮新落结果（发事件用）
        self._baseline = len(self.results)
        # 是否产生了需要写回 state 的变化（游标/状态/重试计数）
        self.dirty = False

    @property
    def _next_index(self) -> int:
        return len(self.results)

    def check_gate(self, target_role: AgentRole) -> str | None:
        """工具层确定性门控：返回 None 放行，否则返回给模型看的 JSON 拒绝理由。"""
        if self.plan.status is not TaskPlanStatus.ACTIVE:
            return json.dumps(
                {
                    "error": (
                        f"任务计划已结束（{self.plan.status.value}），"
                        "不再接受计划内子任务调用"
                    ),
                    "plan_status": self.plan.status.value,
                },
                ensure_ascii=False,
            )
        step = self.plan.steps[self._next_index]
        if step.target_agent is not target_role:
            return json.dumps(
                {
                    "error": (
                        f"当前计划步骤 {step.sequence} 的目标角色是 "
                        f"{step.target_agent.value}，不能调用 "
                        f"{target_role.value}；请按计划顺序执行"
                    ),
                    "expected_target": step.target_agent.value,
                    "current_step_sequence": step.sequence,
                    "current_step_description": step.description,
                },
                ensure_ascii=False,
            )
        return None

    def record_success(self, target_role: AgentRole, output: str) -> None:
        """成功完成当前步骤：落结果、推进游标，最后一步转 COMPLETED。"""
        step = self.plan.steps[self._next_index]
        self.results.append(
            TaskStepResult(
                step_sequence=step.sequence,
                target_agent=step.target_agent,
                success=True,
                output=output or None,
            )
        )
        next_index = self._next_index
        self.plan = self.plan.model_copy(
            update={
                "current_step_index": next_index,
                "status": (
                    TaskPlanStatus.COMPLETED
                    if next_index == len(self.plan.steps)
                    else TaskPlanStatus.ACTIVE
                ),
                "retries_used": self.retries_used,
            }
        )
        self.dirty = True

    def record_failure(
        self,
        target_role: AgentRole,
        error_code: ErrorCode = ErrorCode.AGENT_OUTPUT_INVALID,
    ) -> None:
        """当前步骤失败：按 on_failure 策略处置（语义见类注释与 state.py）。"""
        step = self.plan.steps[self._next_index]
        if (
            step.on_failure == "retry"
            and self.retries_used < _TOOL_PLAN_RETRY_BUDGET
        ):
            # 重试不推进游标、不落中间失败结果：同目标可再次调用，
            # 由下次调用的成败决定步骤终态。
            self.retries_used += 1
            self.dirty = True
            return
        self.results.append(
            TaskStepResult(
                step_sequence=step.sequence,
                target_agent=step.target_agent,
                success=False,
                output=None,
                error_code=error_code,
            )
        )
        if step.on_failure == "continue":
            next_index = self._next_index
            self.plan = self.plan.model_copy(
                update={
                    "current_step_index": next_index,
                    "status": (
                        TaskPlanStatus.COMPLETED
                        if next_index == len(self.plan.steps)
                        else TaskPlanStatus.ACTIVE
                    ),
                    "retries_used": self.retries_used,
                }
            )
        else:
            # abort（含 retry 预算耗尽收口）：计划 FAILED，后续
            # ask_* 被 check_gate 硬熔断。
            self.plan = self.plan.model_copy(
                update={
                    "status": TaskPlanStatus.FAILED,
                    "retries_used": self.retries_used,
                }
            )
        self.dirty = True

    def newly_recorded_results(self) -> list[TaskStepResult]:
        """本轮新落的终局结果（供 _wrap 逐一发 TASK_RESULT_ARCHIVED）。"""
        return self.results[self._baseline :]


@tool("create_task_plan", args_schema=_TaskPlanInput)
def create_task_plan_tool_mode(steps: list[TaskPlanStep]) -> str:
    """为需要至少两个有序子任务的复杂请求创建一次任务计划。"""
    # tool 模式专用变体：一轮至多创建一次计划（S5-A1 冲突语义，审查
    # 加固）——只要 holder 内已有执行对象（无论 ACTIVE/COMPLETED/FAILED），
    # 同轮再次创建一律拒绝。为什么不限缩到 ACTIVE：同轮内已完成计划的
    # 结果与 TASK_RESULT_ARCHIVED 事件已落账，若允许替换会让旧结果静默
    # 蒸发；跨轮重建不受影响（新轮 holder 重新从 state 构建，终态计划
    # 不会进入 holder）。返回模型可读的 JSON 提示而非抛错——模型有机会
    # 纠偏。双保险：即便门控被绕过，_wrap 的 replacing_plan 拦截仍会兜底。
    execution = _TOOL_PLAN_EXECUTION.get()
    current = execution.execution if execution is not None else None
    if current is not None:
        return json.dumps(
            {
                "error": (
                    "本轮已创建过任务计划，不允许重复创建；"
                    "请继续执行或整合当前计划的结果"
                ),
                "plan_status": current.plan.status.value,
                "plan_steps": len(current.plan.steps),
                "completed_steps": current.plan.current_step_index,
            },
            ensure_ascii=False,
        )
    plan = TaskPlan(steps=steps)
    # 安装/替换轮内执行上下文：使同一 ReAct 轮内后续的 ask_* 立即受门控
    # 并逐步记账（模型惯例是创建计划后紧接着开始执行，不能等下一轮）。
    # 写入的是 holder 盒子内容（引用共享，跨线程可见），不是 ContextVar
    # 重绑定——后者在工具线程的快照上下文里会丢失。
    if execution is not None:
        execution.execution = _ToolModePlanExecution(plan, [])
    return plan.model_dump_json()


class _IntentInput(BaseModel):
    """仅暴露给模型的意图分类输入，intent 取值由 Intent 枚举严格约束。

    extra="forbid" 防止模型夹带任意字段（与 _TaskPlanInput 同一约定），
    非法意图值会在工具执行层被 TOOL_INVALID_ARGUMENTS 拒绝，
    不会进入 ToolResult 审计记录。
    reason 不设长度硬约束：超长理由由工具函数截断（见 detect_intent），
    避免 schema 校验失败导致整个意图识别丢失。
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    reason: str = ""


@tool(args_schema=_IntentInput)
def detect_intent(intent: Intent, reason: str = "") -> str:
    """识别当前用户请求的教学意图，返回分类标签（决策前必调）。"""
    # 注意：LangChain 工具执行时把 args 原样传入（intent 是字符串而非
    # Intent 实例），因此这里用 Intent(intent) 显式转换——schema 校验
    # 已保证值是合法枚举，此转换不会失败，且保证输出永远是规范值。
    # reason 截断到 200 字符：审计字段有界（ToolResult.output 不膨胀），
    # 且超长理由不会让工具调用失败、意图识别丢失。
    # 返回 JSON 而非裸枚举值：ToolResult.output 是审计记录，
    # JSON 里同时保留意图与理由，_intent_from_results 只取 intent 字段。
    return json.dumps(
        {"intent": Intent(intent).value, "reason": reason[:200]},
        ensure_ascii=False,
    )


class _LevelInput(BaseModel):
    """仅暴露给模型的水平识别输入，level 取值由 StudentLevel 枚举严格约束。

    与 _IntentInput 同一约定：extra="forbid" 防止模型夹带任意字段，
    非法水平值会在工具执行层被 TOOL_INVALID_ARGUMENTS 拒绝，
    不会进入 ToolResult 审计记录。
    reason 不设长度硬约束：超长理由由工具函数截断（见 detect_level），
    避免 schema 校验失败导致整个水平识别丢失。
    """

    model_config = ConfigDict(extra="forbid")

    level: StudentLevel
    reason: str = ""


@tool(args_schema=_LevelInput)
def detect_level(level: StudentLevel, reason: str = "") -> str:
    """识别或更新学生水平画像（学生自报基础/进阶时调用），返回分类标签。"""
    # 与 detect_intent 同构：LangChain 工具执行时 args 里的 level 是
    # 字符串而非 StudentLevel 实例，这里显式转换保证输出永远是规范值
    # （schema 校验已保证值合法，转换不会失败）。
    # reason 截断到 200 字符：审计字段有界，超长理由不丢失水平识别。
    # 返回 JSON 而非裸枚举值：ToolResult.output 是审计记录，JSON 里同时
    # 保留水平与理由，_level_from_results 只取 level 字段。
    return json.dumps(
        {"level": StudentLevel(level).value, "reason": reason[:200]},
        ensure_ascii=False,
    )


class _EvaluationInput(BaseModel):
    """仅暴露给模型的评价输入，verdict 与两个维度取值由枚举严格约束。

    与 _IntentInput 同一约定：extra="forbid" 防止模型夹带任意字段，
    非法评价值会在工具执行层被 TOOL_INVALID_ARGUMENTS 拒绝，
    不会进入 ToolResult 审计记录。
    reason 不设长度硬约束：超长理由由工具函数截断（见 submit_evaluation），
    避免 schema 校验失败导致整个评价丢失。

    三个结论字段为何共用 EvaluationVerdict 枚举：总结论（verdict）与
    单维度结论（fact_accuracy / citation_completeness）语义一致
    （通过/存疑/不通过），复用同一枚举避免平行定义；verdict 与维度
    之间的一致性（如总评 fail 时维度不应全 pass）不做硬校验——骨架期
    最小可用，写入端只保证枚举合法，语义一致性留给 reason 与审计对照。
    """

    model_config = ConfigDict(extra="forbid")

    verdict: EvaluationVerdict
    fact_accuracy: EvaluationVerdict
    citation_completeness: EvaluationVerdict
    reason: str = ""


@tool(args_schema=_EvaluationInput)
def submit_evaluation(
    verdict: EvaluationVerdict,
    fact_accuracy: EvaluationVerdict,
    citation_completeness: EvaluationVerdict,
    reason: str = "",
) -> str:
    """提交对最终回答的结构化评价结论（评价 Agent 专用，决策后必调）。"""
    # 与 detect_intent 同构：LangChain 工具执行时 args 里的枚举值是
    # 字符串而非枚举实例，这里显式转换保证输出永远是规范值（schema
    # 校验已保证值合法，转换不会失败）。
    # reason 截断到 200 字符：审计字段有界（ToolResult.output 不膨胀），
    # 且超长理由不会让评价丢失；理由可能含被评价内容细节，事件层只取
    # verdict 摘要（脱敏见 events.py），完整 reason 存 state["evaluation"]。
    # 返回 JSON 而非裸枚举值：ToolResult.output 是审计记录，JSON 里同时
    # 保留总结论、两维度结论与理由，_evaluation_from_results 解析取用。
    return json.dumps(
        {
            "verdict": EvaluationVerdict(verdict).value,
            "fact_accuracy": EvaluationVerdict(fact_accuracy).value,
            "citation_completeness": EvaluationVerdict(citation_completeness).value,
            "reason": reason[:200],
        },
        ensure_ascii=False,
    )


# ── 六大功能 P2-9：批改工具 ──────────────────────────────────


class _ObjectiveItemInput(BaseModel):
    """一道客观题的确定性批改输入（P2-9；pi 审查 🟡6：answer_source
    披露标准答案来源，防止「模型自答自批」被误认为确定性评分）。

    extra="forbid" 与 detect_intent 三件套同一约定。answer_source：
    provided = 教师/用户提供的标准答案（消息或附件材料中）；
    generated = 模型检索佐证后生成的参考答案（须如实标注）。
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=120)
    standard_answer: str = Field(min_length=1, max_length=500)
    student_answer: str = Field(default="", max_length=2000)
    max_score: float = Field(gt=0)
    answer_source: Literal["provided", "generated"] = "provided"


class _ObjectiveGradingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_ObjectiveItemInput] = Field(min_length=1, max_length=50)


def _normalize_answer(text: str) -> str:
    """客观题答案归一化：去全部空白、转小写（确定性比对的预处理）。"""
    return re.sub(r"\s+", "", text).lower()


def _answers_match(standard_answer: str, student_answer: str) -> bool:
    """确定性比对规则（零 LLM、可单测、评分可复现）：

    1. 归一化（去全部空白 + 转小写）后严格相等 → 正确；
    2. 学生答案为空 → 错误。

    为什么不做多选「集合相等」容忍（审查 W1 复盘）：由 a-h 字母组成
    的英文单词（face/bad/cab…）与多选答案在形态上**本质不可区分**
    （bad 既可以是拼写题答案也可以是合法多选），任何形态守卫都无法
    兼容两者；而集合比较一旦误判（拼写题 "cafe" 判为正确），错误
    结论会经 submit_grading 确定性落库 learning_records，污染学情
    诊断且无法事后区分——误判代价比「顺序容忍」收益高得多。多选
    乱序作答（"ba" vs "AB"）由模型在调用本工具前规范化学生答案
    解决（evaluator 角色卡约定客观题先整理作答内容）。
    """
    normalized_standard = _normalize_answer(standard_answer)
    normalized_student = _normalize_answer(student_answer)
    if not normalized_student:
        return False
    return normalized_standard == normalized_student


@tool(args_schema=_ObjectiveGradingInput)
def grade_objective_answers(items: list[_ObjectiveItemInput]) -> str:
    """自动批阅客观题：按标准答案确定性比对，逐题给出对错与得分。"""
    # 纯确定性逻辑，零 LLM：比对规则见 _answers_match 注释。
    results = []
    for item in items:
        correct = _answers_match(item.standard_answer, item.student_answer)
        results.append(
            {
                "question_id": item.question_id,
                "correct": correct,
                "score": float(item.max_score) if correct else 0.0,
                "max_score": item.max_score,
                "answer_source": item.answer_source,
            }
        )
    correct_count = sum(1 for result in results if result["correct"])
    return json.dumps(
        {
            "items": results,
            "correct_count": correct_count,
            "total_count": len(results),
        },
        ensure_ascii=False,
    )


class _GradingItemInput(BaseModel):
    """一道题的批改结论输入（P2-9）。

    knowledge_point/error_tag 供 P2-10 确定性落库 learning_records
    （学情诊断的主要数据源，pi 审查 🔴3）；feedback 承载评分依据与
    改进建议（赛题要求），截断有界。score 不得超 max_score（schema
    层即拒，避免脏数据进审计链）。
    """

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=120)
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    # feedback 不设 Field 长度上限（审查 S3：与 EvaluationResult.reason
    # 同一单口径——超长由下方 validator 截断而非 schema 拒绝，避免
    # 声明与实施的双口径误导容量推导）。
    feedback: str = ""
    knowledge_point: str | None = Field(default=None, max_length=120)
    error_tag: str | None = Field(default=None, max_length=60)

    @field_validator("feedback")
    @classmethod
    def feedback_must_be_bounded(cls, feedback: str) -> str:
        return feedback[:300]

    @model_validator(mode="after")
    def score_must_not_exceed_max(self) -> _GradingItemInput:
        if self.score > self.max_score:
            raise ValueError("score must not exceed max_score")
        return self


class _GradingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_GradingItemInput] = Field(min_length=1, max_length=50)
    # overall_comment 不设 Field 上限，由工具函数 [:500] 截断
    #（审查 S3：单口径，与 reason 先例一致）。
    overall_comment: str = ""


@tool(args_schema=_GradingInput)
def submit_grading(
    items: list[_GradingItemInput], overall_comment: str = ""
) -> str:
    """提交一次作业/试题批改的结构化结论（评价 Agent 专用，批改完成后必调）。"""
    # 总分由核心侧确定性汇总（不信任模型自报总分，与 evidence_tool_names
    # 「证据由核心侧确定」同一哲学）；_grading_from_results 解析本 JSON
    # 构造 GradingResult 写通道，并逐题确定性落库（P2-10）。
    total_score = sum(item.score for item in items)
    max_total_score = sum(item.max_score for item in items)
    return json.dumps(
        {
            "items": [item.model_dump(mode="json") for item in items],
            "overall_comment": overall_comment[:500],
            "total_score": total_score,
            "max_total_score": max_total_score,
        },
        ensure_ascii=False,
    )


# ── S5-A3 子代理上下文增强的有界参数 ──────────────────────
# 最近对话注入量刻意有界：子代理是执行者不是对话者，只需要「多轮
# 追问指代的是什么」这一最小上下文；无界携带既费 token 又稀释任务
# 消息的注意力。
_SUBAGENT_CONTEXT_MESSAGE_LIMIT = 4
_SUBAGENT_CONTEXT_MESSAGE_MAX_CHARS = 1000
_SUBAGENT_CONTEXT_TOTAL_MAX_CHARS = (
    _SUBAGENT_CONTEXT_MESSAGE_LIMIT * _SUBAGENT_CONTEXT_MESSAGE_MAX_CHARS
)


def _recent_context_messages(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """取父状态最近的人类/AI 文本消息（有界），按时间正序返回。

    边界：剔除工具消息与空消息；每条截断到单条上限，总量另有累计上限
    （双保险，防止单条上限内仍堆满长文）；只保留纯文本 content（列表型
    content 的多模态消息不适用于子代理的纯文本上下文）。
    返回新构造的消息副本（截断后的内容），不共享父状态消息实例。
    """
    picked: list[BaseMessage] = []
    total_chars = 0
    for message in reversed(messages):
        if len(picked) >= _SUBAGENT_CONTEXT_MESSAGE_LIMIT:
            break
        if not isinstance(message, (HumanMessage, AIMessage)):
            continue
        content = message.content
        if not isinstance(content, str) or not content.strip():
            continue
        truncated = content[:_SUBAGENT_CONTEXT_MESSAGE_MAX_CHARS]
        if total_chars + len(truncated) > _SUBAGENT_CONTEXT_TOTAL_MAX_CHARS:
            break
        total_chars += len(truncated)
        if isinstance(message, HumanMessage):
            picked.append(HumanMessage(content=truncated))
        else:
            picked.append(AIMessage(content=truncated))
    picked.reverse()
    return picked


class CollaborativeAgentGraph:
    """注册四个同构 ReAct Agent，并负责它们之间的路由。"""

    def __init__(
        self,
        *,
        model: ChatModel,
        tools: Sequence[BaseTool] = (),
        max_iterations: int = 5,
        max_tool_calls: int = 20,
        max_context_messages: int | None = None,
        max_context_tokens: int | None = None,
        context_token_counter: MessageTokenCounter | None = None,
        tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        tool_timeouts: Mapping[str, float] | None = None,
        tool_permissions: Mapping[str, Collection[AgentRole]] | None = None,
        max_handoffs: int = 4,
        max_agent_switches: int = 8,
        checkpointer: BaseCheckpointSaver[str] | None = None,
        interrupt_before_handoff: bool = False,
        orchestration_mode: OrchestrationMode = "handoff",
        learning_store: LearningRecordStore | None = None,
        # 固定工作流开关（lesson-workflow-design §九）：仅 tool 模式可
        # 启用——工作流走图节点确定性调度，与 handoff 的 next_agent 路由
        # 语义冲突；关闭时不注册 start_workflow 工具、_route 分支对
        # workflow=None 恒假，行为与未引入该特性逐字节等价。
        enable_workflows: bool = False,
    ) -> None:
        # 参数校验：尽早拒绝非法组合，避免图运行期才暴露配置错误
        approval_tool_names = frozenset(
            business_tool.name
            for business_tool in tools
            if isinstance(
                extras := getattr(business_tool, "extras", None),
                Mapping,
            )
            and extras.get("requires_approval") is True
        )
        if max_handoffs <= 0:
            raise ValueError("max_handoffs must be positive")
        if max_agent_switches <= 0:
            raise ValueError("max_agent_switches must be positive")
        if interrupt_before_handoff and checkpointer is None:
            raise ValueError(
                "interrupt_before_handoff requires a configured checkpointer"
            )
        if approval_tool_names and checkpointer is None:
            raise ValueError("approval-gated tools require a configured checkpointer")
        if orchestration_mode not in {"handoff", "tool"}:
            raise ValueError("orchestration_mode must be 'handoff' or 'tool'")
        if enable_workflows and orchestration_mode != "tool":
            raise ValueError("enable_workflows requires orchestration_mode='tool'")
        if orchestration_mode == "tool" and interrupt_before_handoff:
            raise ValueError(
                "tool orchestration does not support interrupt_before_handoff"
            )

        # 权限配置校验：每个业务工具都必须有显式角色白名单（None 视为配置错误）
        permissions = tool_permissions or {}
        business_tool_names = {business_tool.name for business_tool in tools}
        permission_names = set(permissions)
        unknown_permissions = permission_names - business_tool_names
        if unknown_permissions:
            names = ", ".join(sorted(unknown_permissions))
            raise ValueError(f"tool_permissions 包含非业务工具：{names}")
        missing_permissions = business_tool_names - permission_names
        if missing_permissions:
            names = ", ".join(sorted(missing_permissions))
            raise ValueError(f"tool_permissions 缺少业务工具：{names}")
        none_permissions = {
            name for name in business_tool_names if permissions[name] is None
        }
        if none_permissions:
            names = ", ".join(sorted(none_permissions))
            raise ValueError(f"tool_permissions 不允许业务工具权限为 None：{names}")

        registry = ToolRegistry()
        self.orchestration_mode = orchestration_mode
        if orchestration_mode == "handoff":
            registry.register(handoff, allowed_roles={AgentRole.SUPERVISOR})
            registry.register(
                create_task_plan,
                allowed_roles={AgentRole.SUPERVISOR},
            )
        else:
            for target_role, tool_name in _SUBAGENT_TOOL_NAMES.items():
                registry.register(
                    self._create_subagent_tool(target_role, tool_name),
                    allowed_roles={AgentRole.SUPERVISOR},
                )
            # S5-A1：tool 模式同样开放计划能力（生产点亮）。用带冲突
            # 门控的变体而非 handoff 共用工具：已有 ACTIVE 计划时工具层
            # 拒绝并给模型可读提示，而不是像 handoff 那样靠 _wrap 轮末
            # fail 硬收口（tool 模式的模型有机会先收口当前计划再重建）。
            registry.register(
                create_task_plan_tool_mode,
                allowed_roles={AgentRole.SUPERVISOR},
            )
            # 固定工作流触发工具（lesson-workflow-design §二）：仅
            # enable_workflows 时注册——未启用时工具不存在，模型无从
            # 触发，路由分支对 workflow=None 恒假，零行为差异。
            if enable_workflows:
                registry.register(
                    self._create_start_workflow_tool(),
                    allowed_roles={AgentRole.SUPERVISOR},
                )
        # S2-T1 意图识别：detect_intent 仅 Supervisor 可用，
        # 与 handoff / create_task_plan 一样由模型在 ReAct 循环中调用。
        registry.register(
            detect_intent,
            allowed_roles={AgentRole.SUPERVISOR},
        )
        # S2-T2 学生水平画像：detect_level 仅 Supervisor 可用（与
        # detect_intent 同一约定），由模型在学生自报水平时调用。
        registry.register(
            detect_level,
            allowed_roles={AgentRole.SUPERVISOR},
        )
        # ── 固定工作流确定性导出工具（2026-08-29 探索结论）──
        # 仅 enable_workflows 且装配了 officecli 时注册：教案正文经
        # step_outputs 暂存，由本工具确定性写入 docx 并自验——模型不搬
        # 运正文（CLI 转义/长度/迭代预算三重脆弱，真实冒烟两次空文件）。
        if enable_workflows:
            _office_edit = next(
                (
                    business_tool
                    for business_tool in tools
                    if getattr(business_tool, "name", "") == "officecli_edit"
                ),
                None,
            )
            _office_inspect = next(
                (
                    business_tool
                    for business_tool in tools
                    if getattr(business_tool, "name", "")
                    == "officecli_inspect"
                ),
                None,
            )
            if _office_edit is not None and _office_inspect is not None:
                registry.register(
                    self._create_export_workflow_docx_tool(
                        _office_edit,
                        _office_inspect,
                    ),
                    allowed_roles={
                        AgentRole.TEACHING_ASSISTANT,
                        AgentRole.LEARNING_ASSISTANT,
                        AgentRole.EVALUATOR,
                    },
                )
                from .workflows.ppt_export import (
                    create_export_workflow_pptx_tool,
                )

                registry.register(
                    create_export_workflow_pptx_tool(
                        _office_edit,
                        _office_inspect,
                        parent_state=_ACTIVE_PARENT_STATE,
                    ),
                    allowed_roles={
                        AgentRole.TEACHING_ASSISTANT,
                        AgentRole.LEARNING_ASSISTANT,
                        AgentRole.EVALUATOR,
                    },
                )
        # S2-T3 基础评价规则：submit_evaluation 仅 evaluator 可用，
        # 由评价 Agent 在 ReAct 循环中基于最终回答与检索证据调用
        # （prompt 约定，见 ROLE_PROMPTS[EVALUATOR]），结果经 _wrap
        # 解析写入 state["evaluation"] 并发 EVALUATION_COMPLETED 事件。
        registry.register(
            submit_evaluation,
            allowed_roles={AgentRole.EVALUATOR},
        )
        # ── P2-9 批改工具（仅 evaluator；与 submit_evaluation 同一
        # 构造器内注册约定，不进 app.py 业务权限矩阵）──
        # 客观题确定性比对工具 + 结构化批改提交工具；批改结果的通道
        # 写入与学习记录落库由 _wrap 确定性完成（P2-10），不靠模型自觉。
        registry.register(
            grade_objective_answers,
            allowed_roles={AgentRole.EVALUATOR},
        )
        registry.register(
            submit_grading,
            allowed_roles={AgentRole.EVALUATOR},
        )
        # ── P0-5 学习记录工具（条件注册，pi 三轮审查 🔴1/🔴2 修复）──
        # 注册路径唯一：仿 submit_evaluation 先例在构造器内注册、不进
        # app.py 业务权限矩阵（权限校验会把不在 tools 列表里的声明
        # 拒为「非业务工具」，两条路径互斥）；闭包工具捕获
        # self._learning_store（模块级 @tool 拿不到 per-graph 实例）。
        # 条件注册红线：仅当 learning_store 非 None 时注册——
        # test_graph_accepts_empty_tools_and_permissions 对 registry
        # 工具清单做精确列表断言，无条件注册必击穿「无 store 注入时
        # 既有测试零改动」验收；工具不存在即不可被调用，None 容忍
        # 只保留在 _wrap 落库守卫一处。
        self._learning_store = learning_store
        if learning_store is not None:
            registry.register(
                self._create_record_learning_outcome_tool(),
                allowed_roles={
                    AgentRole.LEARNING_ASSISTANT,
                    AgentRole.EVALUATOR,
                },
            )
            registry.register(
                self._create_get_learning_records_tool(),
                allowed_roles={
                    AgentRole.LEARNING_ASSISTANT,
                    AgentRole.EVALUATOR,
                },
            )
        # 业务工具：按外部权限白名单注册（未授权角色调用会被工具层拒绝）
        for business_tool in tools:
            registry.register(
                business_tool,
                allowed_roles=permissions.get(business_tool.name),
            )
        self.registry = registry
        self._approval_tool_names = approval_tool_names
        self.max_handoffs = max_handoffs
        self.max_agent_switches = max_agent_switches
        # 固定工作流（lesson-workflow-design）：注册表来自 core.workflows，
        # 启用前提已在参数校验段保证（仅 tool 模式）。
        self.enable_workflows = enable_workflows
        self.checkpointer = checkpointer
        self.interrupt_before_handoff = interrupt_before_handoff
        self._persistence_lock = RLock()
        effective_tool_timeouts = dict(tool_timeouts or {})
        if orchestration_mode == "tool":
            for tool_name in _SUBAGENT_TOOL_NAMES.values():
                effective_tool_timeouts.setdefault(
                    tool_name,
                    _SUBAGENT_TOOL_TIMEOUT_SECONDS,
                )
        # 创建4个同构的agent，均遵循react设计范式
        self.agents = create_agent_nodes(
            model=model,
            registry=registry,
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
            max_context_messages=max_context_messages,
            max_context_tokens=max_context_tokens,
            context_token_counter=context_token_counter,
            tool_timeout_seconds=tool_timeout_seconds,
            tool_timeouts=effective_tool_timeouts,
            prompt_overrides=(
                {
                    AgentRole.SUPERVISOR: (
                        TOOL_ORCHESTRATION_SUPERVISOR_PROMPT
                        + (
                            WORKFLOW_SUPERVISOR_CLAUSE
                            if enable_workflows
                            else ""
                        )
                    )
                }
                if orchestration_mode == "tool"
                else None
            ),
        )

        # 图缓存，避免重复编译
        self._app: CompiledAgentGraph | None = None

    def _create_subagent_tool(
        self,
        target_role: AgentRole,
        tool_name: str,
    ) -> BaseTool:
        """创建一个会等待目标 Agent 完成并返回结果的同步工具。

        S5-A1/A2：tool 模式下若存在活跃计划（经 _TOOL_PLAN_EXECUTION
        注入），调用受确定性门控约束——目标必须等于当前步骤的
        target_agent，完成后按步骤落 TaskStepResult 并推进游标，失败按
        on_failure 策略处置。无计划（execution is None）时行为与既往
        完全一致（零回归）。
        """

        def invoke_subagent(task: str) -> str:
            holder = _TOOL_PLAN_EXECUTION.get()
            execution = holder.execution if holder is not None else None
            if execution is not None:
                gate_error = execution.check_gate(target_role)
                if gate_error is not None:
                    # 返回 JSON 拒绝理由而非抛错：模型能读到期望目标并
                    # 自行纠偏（与 record_learning_outcome 的 JSON 语义
                    # 同一模式）；门控拒绝不产生子代理运行，零成本。
                    return gate_error
            try:
                result = self._run_subagent(target_role, task)
            except _SubagentRunError as exc:
                # 子代理运行失败：按计划失败策略记录后原样上抛（ReAct
                # 层把异常转成失败工具结果，模型可见）。无计划时与既往
                # 语义完全一致。
                if execution is not None and execution.plan.status is TaskPlanStatus.ACTIVE:
                    execution.record_failure(target_role, exc.error_code)
                raise
            if execution is not None and execution.plan.status is TaskPlanStatus.ACTIVE:
                execution.record_success(target_role, output=result)
            return result

        return tool(
            tool_name,
            args_schema=_SubagentTaskInput,
            description=(
                f"将一个边界清晰的任务交给 {target_role.value}，"
                "等待完成后返回可供主智能体整合的结果。"
            ),
            extras={"subagent": True},
        )(invoke_subagent)

    def _create_start_workflow_tool(self) -> BaseTool:
        """创建固定工作流触发工具（lesson-workflow-design §二）。

        模型只负责确认意图与填参数（topic/grade_level），步骤顺序、
        预算、失败策略全部来自注册表定义——模型不可自造。返回值是
        WorkflowState 的 JSON：_wrap 在轮末解析写回 state["workflow"]
        （与 create_task_plan 的结果回传-轮末解析机制同一模式），本轮
        结束后路由进 _workflow_dispatch 确定性调度。
        """

        class _StartWorkflowInput(BaseModel):
            model_config = ConfigDict(extra="forbid")

            workflow_id: str = Field(min_length=1, max_length=60)
            topic: str = Field(min_length=1, max_length=120)
            grade_level: str | None = Field(default=None, max_length=60)
            # 工作流声明的额外参数（ppt-workflow-design §五-3）：键必须
            # 在定义的 extra_params 白名单内（build_state 拒绝未声明键），
            # 值 ≤200 字符；写状态前经定义的 param_normalizer 确定性规整。
            params: dict[str, str] | None = Field(default=None)

        def start_workflow(
            workflow_id: str,
            topic: str,
            grade_level: str | None = None,
            params: dict[str, str] | None = None,
        ) -> str:
            definition = get_workflow(workflow_id)
            if definition is None:
                return json.dumps(
                    {
                        "error": "未知的工作流 id，请使用已注册工作流",
                        "registered": registered_workflow_ids(),
                    },
                    ensure_ascii=False,
                )
            parent = _ACTIVE_PARENT_STATE.get()
            workspace_root = (
                None if parent is None else parent.get("workspace_root")
            )
            if not isinstance(workspace_root, str) or not workspace_root.strip():
                return json.dumps(
                    {
                        "error": (
                            "当前会话未绑定工作区，无法创建产物目录；"
                            "请先确认会话工作区后再启动工作流"
                        )
                    },
                    ensure_ascii=False,
                )
            # 同轮重复启动防御：_ACTIVE_PARENT_STATE 是轮首快照，_wrap
            # 的写回发生在轮末——工具内读到的 workflow 恒为轮首值
            # （正常为 None）。真正的同轮去重靠 _wrap 只采纳最后一个
            # 解析结果 + 空 run_id 目录隔离，双启动只浪费一个空目录。
            run_id = None if parent is None else parent.get("run_id")
            artifact_root = Path(workspace_root) / ".workflow-artifacts" / str(
                run_id or uuid4()
            )
            try:
                artifact_root.mkdir(parents=True, exist_ok=True)
                params_merged: dict[str, str] = {
                    "topic": topic.strip(),
                    "grade_hint": (
                        f"（对象：{grade_level.strip()}）"
                        if grade_level and grade_level.strip()
                        else ""
                    ),
                }
                for key, value in (params or {}).items():
                    key = key.strip()
                    value = str(value).strip()
                    if not key or len(value) > 200:
                        return json.dumps(
                            {
                                "error": (
                                    f"工作流参数不合法：键 {key!r} 为空或"
                                    "值超过 200 字符"
                                )
                            },
                            ensure_ascii=False,
                        )
                    params_merged[key] = value
                workflow_state = definition.build_state(
                    params_merged,
                    artifact_root=str(artifact_root),
                )
            except ValueError as exc:
                return json.dumps(
                    {"error": f"工作流参数不合法：{exc}"},
                    ensure_ascii=False,
                )
            # 返回值 = WorkflowState JSON + 行为指令。指令放工具结果里
            # 而不是只放角色卡：模型对「工具结果内的指令」遵循度显著更
            # 高（真实模型冒烟：仅角色卡约束时，模型启动工作流后继续
            # 反复调工具直至迭代超限）。JSON 解析由 _workflow_from_results
            # 的宽容读取兼容（raw_decode 取首个 JSON 对象）。
            return (
                workflow_state.model_dump_json()
                + "\n[系统] 工作流已启动，步骤将由系统按序自动执行。"
                "请立即输出一句简短确认（告知用户工作流已开始与包含的"
                "步骤），然后结束本轮；不要再调用任何工具，重复启动会被"
                "系统拒绝。"
            )

        return tool(
            "start_workflow",
            args_schema=_StartWorkflowInput,
            description=(
                "启动一个注册过的固定工作流（如教案制作）。步骤顺序由系统"
                "确定性执行：启动后各专业 Agent 将按序自动完成，无需再调用"
                " ask_*；你只需在全部步骤完成后整合结果作答。"
            ),
        )(start_workflow)

    def _create_export_workflow_docx_tool(
        self,
        office_edit: BaseTool,
        office_inspect: BaseTool,
    ) -> BaseTool:
        """工作流产物确定性导出工具（2026-08-29 探索结论）。

        为什么存在：让模型把整篇正文经 CLI 参数搬运进 officecli 是三重
        脆弱设计——语法发现耗迭代（load_skill 技能名不匹配）、命令长度
        受 MAX_COMMAND_TOKENS 限制、预算耗尽后模型谎报完成（真实冒烟两
        次产出空 docx）。本工具把写入变成确定性代码路径：

        1. 从 state.workflow.step_outputs 读取暂存的教案全文（模型不搬
           运正文，工具无内容参数）；
        2. 暂存文本落盘 draft.md，再以代码构造的命令
           `create docx` + `add --type markdown --prop src=draft.md`
           写入（正文从文件读取，officecli 的 markdown 元素原生渲染
           标题/段落/列表）；
        3. 写入后强制自验（view stats 段落数为 0 即报错），杜绝谎报。

        写入以 approved_office_execution 上下文执行：这是工作流自身的
        确定性产物写入（产物区边界由构造保证），不是模型发起的写操作。
        """
        from .tools.office_tools import approved_office_execution

        class _ExportInput(BaseModel):
            model_config = ConfigDict(extra="forbid")

            filename: str | None = Field(default=None, max_length=120)

        def export_workflow_docx(filename: str | None = None) -> str:
            parent = _ACTIVE_PARENT_STATE.get()
            raw_workflow = None if parent is None else parent.get("workflow")
            workflow: WorkflowState | None = None
            if raw_workflow is not None:
                try:
                    workflow = WorkflowState.model_validate(raw_workflow)
                except ValidationError:
                    workflow = None
            if (
                workflow is None
                or not workflow.artifact_root
                or not workflow.step_outputs.get("draft", "").strip()
            ):
                return json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "暂存区没有教案全文（draft 步骤输出为空或无产物"
                            "目录），无法导出"
                        ),
                    },
                    ensure_ascii=False,
                )
            root = Path(workflow.artifact_root)
            draft_text = workflow.step_outputs["draft"]
            md_path = root / "draft.md"
            md_path.write_text(draft_text, encoding="utf-8")
            topic = workflow.params.get("topic", "教案")
            raw_name = filename or f"教案-{topic}.docx"
            docx_path = root / sanitize_artifact_filename(raw_name)

            def _invoke(tool: BaseTool, command: list[str]) -> dict[str, Any]:
                raw = tool.invoke({"command": command})
                parsed: Any = raw
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        parsed = {"ok": False, "message": raw[:300]}
                return cast(dict[str, Any], parsed)

            with approved_office_execution():
                created = _invoke(office_edit, ["create", str(docx_path)])
                # create 目标已存在等场景：视为可继续（幂等写入）
                if not created.get("ok") and "exists" not in str(
                    created.get("message", "")
                ).lower():
                    return json.dumps(
                        {
                            "ok": False,
                            "error": f"创建文档失败：{str(created)[:300]}",
                        },
                        ensure_ascii=False,
                    )
                written = _invoke(
                    office_edit,
                    [
                        "add",
                        str(docx_path),
                        # add 命令需要 <parent> 位置参数（正文挂载点，
                        # 见 officecli help docx add：Paths: /body）——
                        # 真实冒烟取证：缺它整条命令报
                        # "Required argument missing"，写入静默失败。
                        "/body",
                        "--type",
                        "markdown",
                        "--prop",
                        f"src={md_path}",
                    ],
                )
            if not written.get("ok"):
                return json.dumps(
                    {
                        "ok": False,
                        "error": f"写入教案内容失败：{str(written)[:300]}",
                    },
                    ensure_ascii=False,
                )
            stats = _invoke(
                office_inspect,
                ["view", str(docx_path), "stats"],
            )
            paragraphs = 0
            match = re.search(
                r"Paragraphs:\s*(\d+)", str(stats.get("stdout", "")), re.DOTALL
            )
            if match is not None:
                paragraphs = int(match.group(1))
            if paragraphs <= 0:
                return json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "写入自验失败：文档段落数为 0，正文未写入；"
                            "请勿声称导出成功"
                        ),
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ok": True,
                    "docx": str(docx_path),
                    "paragraphs": paragraphs,
                    "draft_chars": len(draft_text),
                },
                ensure_ascii=False,
            )

        return tool(
            "export_workflow_docx",
            args_schema=_ExportInput,
            description=(
                "把暂存的教案全文（draft 步骤产出）确定性导出为产物目录内"
                "的 docx 文件并自动验证段落数。无需提供正文参数；调用成功"
                "后报告返回值中的文件路径与段落数即可。"
            ),
        )(export_workflow_docx)

    def _create_record_learning_outcome_tool(self) -> BaseTool:
        """创建学习结果记录工具（P0-5 闭包工具，捕获 store 实例）。

        user_id/session_id 从 learning_scope 注入（_wrap.node() 在
        _ACTIVE_PARENT_STATE.set 同位设置）——**模型不可见不可控**，
        防跨用户伪造记录；scope 缺失（非图执行上下文直调）时返回
        recorded=False 而非报错（防御性，正常图执行必有 scope）。
        """
        store = self._learning_store

        class _RecordOutcomeInput(BaseModel):
            model_config = ConfigDict(extra="forbid")

            knowledge_point: str = Field(min_length=1, max_length=120)
            outcome: Literal["correct", "partial", "incorrect"]
            kind: Literal["answer", "diagnosis", "path_plan"] = "answer"
            error_tag: str | None = Field(default=None, max_length=60)

        def record_learning_outcome(
            knowledge_point: str,
            outcome: str,
            kind: str = "answer",
            error_tag: str | None = None,
        ) -> str:
            scope = learning_scope.get()
            user_id = None if scope is None else scope.get("user_id")
            if scope is None or user_id is None or store is None:
                return json.dumps(
                    {"recorded": False, "reason": "no learning scope"},
                    ensure_ascii=False,
                )
            # kind 由 schema Literal 约束为三值（不含 grading——批改
            # 落库由 _wrap 确定性完成，不经过模型工具，见 P2-10）；
            # 枚举合法性在 store 层双保险校验。
            assert outcome in LEARNING_OUTCOMES
            inserted = store.append_record(
                user_id,
                session_id=scope.get("session_id"),
                knowledge_point=knowledge_point.strip(),
                outcome=outcome,
                kind=kind,
                error_tag=(error_tag.strip() if error_tag else None),
            )
            return json.dumps(
                {"recorded": inserted, "knowledge_point": knowledge_point.strip()},
                ensure_ascii=False,
            )

        return tool(
            "record_learning_outcome",
            args_schema=_RecordOutcomeInput,
            description=(
                "记录一次学习结果（知识点、对错、错因标签），"
                "供学情诊断与学习路径规划使用。"
            ),
        )(record_learning_outcome)

    def _create_get_learning_records_tool(self) -> BaseTool:
        """创建学习记录聚合查询工具（P0-5 闭包工具，只读）。"""
        store = self._learning_store

        def get_learning_records() -> str:
            scope = learning_scope.get()
            user_id = None if scope is None else scope.get("user_id")
            if scope is None or user_id is None or store is None:
                return json.dumps(
                    {
                        "user_id": None,
                        "total_attempts": 0,
                        "knowledge_points": [],
                        "weak_points": [],
                    },
                    ensure_ascii=False,
                )
            summary = store.summarize(user_id)
            return json.dumps(
                {"user_id": user_id, **summary}, ensure_ascii=False
            )

        return tool(
            "get_learning_records",
            description=(
                "读取当前学生的作答聚合：知识点尝试次数、正确率、"
                "薄弱点与最近练习时间（学情诊断/路径规划决策前必调）。"
            ),
        )(get_learning_records)

    def _run_subagent(self, target_role: AgentRole, task: str) -> str:
        """在隔离消息上下文中执行专业 Agent，并返回有界结构化结果。

        S5-A3：子代理默认只见任务字符串，但多轮追问（如「再讲细一点」）
        脱离近期对话就无法理解指代——把父状态最近的有界文本对话放在任务
        消息之前，使子代理能看到必要语境；工具消息不含用户意图，剔除。
        """
        parent = _ACTIVE_PARENT_STATE.get()
        child_state = create_initial_state(
            session_id=None if parent is None else parent.get("session_id"),
            user_id=None if parent is None else parent.get("user_id"),
            run_id=None if parent is None else parent.get("run_id"),
            workspace_root=None if parent is None else parent.get("workspace_root"),
            additional_workspace_roots=(
                []
                if parent is None
                else parent.get("additional_workspace_roots", [])
            ),
        )
        context_messages: list[BaseMessage] = []
        if parent is not None:
            context_messages = _recent_context_messages(
                cast(list[BaseMessage], parent.get("messages", []))
            )
        child_state["messages"] = [
            *context_messages,
            HumanMessage(content=task),
        ]
        if parent is not None:
            child_state["level"] = parent.get("level")
            child_state["task_context"] = parent.get("task_context")

        result = self.agents[target_role].run(child_state)
        traces = _SUBAGENT_EVENT_TRACES.get()
        if traces is not None:
            traces.append(
                cast(list[RunEvent], result.updates.get("events", []))
            )
        if result.error is not None:
            raise _SubagentRunError(
                _archivable_step_error(result.error.error_code)
            )
        output = _terminal_agent_output(result.messages)
        if output is None:
            raise _SubagentRunError(ErrorCode.AGENT_OUTPUT_INVALID)
        child_tool_results = cast(
            list[ToolResult], result.updates.get("tool_results", [])
        )
        citations = _citations_from_tool_results(child_tool_results)
        payload: dict[str, Any] = {
            "agent": target_role.value,
            "output": output,
            "found": bool(citations),
            "hits": [
                {"citation": citation.model_dump(mode="json")}
                for citation in citations
            ],
        }
        # ── P2-10 tool 模式结构化回传（批改在生产模式可见的唯一通道）──
        # 子代理直调 run() 不经 _wrap（见模块补缺说明），其 submit_grading
        # 结果只以 JSON 文本回到 Supervisor——这里把解析后的批改结论与
        # **子代理 submit_grading 的 tool_call_id 一并放入负载**（pi 审查
        # 🟡B：Supervisor 轮 _wrap 手里只有 ask_evaluator 的 ToolResult，
        # 不传递则拿不到落库幂等键），由 Supervisor 轮 _wrap 提取写通道。
        grading_pair = _grading_from_results(child_tool_results)
        if grading_pair is not None:
            grading_result, grading_tool_call_id = grading_pair
            payload["grading"] = {
                "result": grading_result.model_dump(mode="json"),
                "tool_call_id": grading_tool_call_id,
            }
        return json.dumps(payload, ensure_ascii=False)

    def build(self) -> CompiledAgentGraph:
        """构建一次并缓存可执行图。"""
        if self._app is not None:
            return self._app

        graph = StateGraph(AgentState)
        # 路由表 = 路由返回值 ： 图节点 （映射）
        routes: dict[Hashable, str] = {
            AgentRole.SUPERVISOR.value: AgentRole.SUPERVISOR.value,
            AgentRole.TEACHING_ASSISTANT.value: AgentRole.TEACHING_ASSISTANT.value,
            AgentRole.LEARNING_ASSISTANT.value: AgentRole.LEARNING_ASSISTANT.value,
            AgentRole.EVALUATOR.value: AgentRole.EVALUATOR.value,
            _TASK_PLAN_DISPATCH_NODE: _TASK_PLAN_DISPATCH_NODE,
            "end": END,
        }
        if self.interrupt_before_handoff:
            routes[_HANDOFF_APPROVAL_NODE] = _HANDOFF_APPROVAL_NODE
        if self._approval_tool_names:
            routes[_TOOL_APPROVAL_NODE] = _TOOL_APPROVAL_NODE
        # 固定工作流调度节点（lesson-workflow-design §二）：仅启用时
        # 注册——关闭时 _route 分支不可达（workflow 恒 None），图结构
        # 与引入前完全一致。
        if self.enable_workflows:
            routes[_WORKFLOW_DISPATCH_NODE] = _WORKFLOW_DISPATCH_NODE

        # 路由函数绑定本图的编排模式（S5-A1：tool 模式活动计划不走分派
        # 节点，见 _route 注释）；lambda 保持 LangGraph 期望的 fn(state) 签名。
        route_fn = lambda state: self._route(state, self.orchestration_mode)

        for role, agent in self.agents.items():
            graph.add_node(role.value, self._wrap(agent))
            graph.add_conditional_edges(role.value, route_fn, routes)

        graph.add_node(_TASK_PLAN_DISPATCH_NODE, self._dispatch_task_plan)
        graph.add_conditional_edges(
            _TASK_PLAN_DISPATCH_NODE,
            route_fn,
            routes,
        )
        if self.enable_workflows:
            graph.add_node(_WORKFLOW_DISPATCH_NODE, self._workflow_dispatch)
            graph.add_conditional_edges(
                _WORKFLOW_DISPATCH_NODE,
                route_fn,
                routes,
            )

        if self.interrupt_before_handoff:
            graph.add_node(_HANDOFF_APPROVAL_NODE, self._approve_handoff)
            graph.add_conditional_edges(
                _HANDOFF_APPROVAL_NODE,
                route_fn,
                routes,
            )

        if self._approval_tool_names:
            graph.add_node(_TOOL_APPROVAL_NODE, self._approve_tool)
            graph.add_conditional_edges(
                _TOOL_APPROVAL_NODE,
                route_fn,
                routes,
            )

        graph.set_entry_point(AgentRole.SUPERVISOR.value)
        self._app = graph.compile(checkpointer=self.checkpointer)
        return self._app

    def _wrap(self, agent: ReActAgentNode) -> Runnable[AgentState, AgentState]:
        """把 ReAct 结果转换为 LangGraph 状态更新。"""

        def node(state: AgentState) -> AgentState:
            # 上游已判死（run_error 非空）：原样透传终止，不让本节点覆盖失败结果
            existing_error = state.get("run_error")
            if existing_error is not None:
                return cast(
                    AgentState,
                    {"next_agent": None, "run_error": existing_error},
                )

            # 防御性校验：上一轮指定的 next_agent 必须是已注册角色，防外部注入非法目标
            existing_target = state.get("next_agent")
            registered_targets = {role.value for role in self.agents}
            if (
                existing_target is not None
                and existing_target not in registered_targets
            ):
                error = RunError(
                    error_code=ErrorCode.GRAPH_INVALID_TARGET,
                    message=f"非法 next_agent：{existing_target}",
                    agent=agent.role.value,
                )
                sequence = max(
                    (event.sequence for event in state.get("events", [])),
                    default=-1,
                )
                return cast(
                    AgentState,
                    {
                        "next_agent": None,
                        "run_error": error,
                        "events": [
                            RunEvent(
                                event_type=EventType.RUN_FAILED,
                                sequence=sequence + 1,
                                session_id=state.get("session_id"),
                                run_id=state.get("run_id"),
                                agent=agent.role.value,
                                success=False,
                                error_code=error.error_code,
                            )
                        ],
                    },
                )

            # 计划 Worker 预检：本节点须是计划当前步骤的目标角色，且结果前缀与游标一致
            preflight_plan, preflight_error = _planned_worker_preflight(
                state,
                agent.role,
            )
            if preflight_error is not None:
                sequence = max(
                    (event.sequence for event in state.get("events", [])),
                    default=-1,
                )
                preflight_updates: dict[str, object] = {
                    "next_agent": None,
                    "run_error": preflight_error,
                    "events": [
                        RunEvent(
                            event_type=EventType.RUN_FAILED,
                            sequence=sequence + 1,
                            session_id=state.get("session_id"),
                            run_id=state.get("run_id"),
                            agent=agent.role.value,
                            success=False,
                            error_code=preflight_error.error_code,
                        )
                    ],
                }
                if preflight_plan is not None:
                    preflight_updates["task_plan"] = preflight_plan.model_copy(
                        update={"status": TaskPlanStatus.FAILED}
                    )
                return cast(AgentState, preflight_updates)

            # Supervisor 轮：计划已完成则把子任务结果拼成消息注入上下文，供模型聚合作答
            aggregation_results: list[TaskStepResult] | None = None
            run_state = state
            if agent.role is AgentRole.SUPERVISOR:
                try:
                    aggregation_results = _ready_task_results(state)
                except ValueError:
                    plan = _task_plan_from_state(state)
                    error = RunError(
                        error_code=ErrorCode.GRAPH_AGGREGATION_INVALID,
                        message="任务结果与计划不一致，无法安全聚合",
                        agent=agent.role.value,
                    )
                    sequence = max(
                        (event.sequence for event in state.get("events", [])),
                        default=-1,
                    )
                    aggregation_failure_updates: dict[str, object] = {
                        "next_agent": None,
                        "run_error": error,
                        "events": [
                            RunEvent(
                                event_type=EventType.TASK_RESULTS_AGGREGATED,
                                sequence=sequence + 1,
                                session_id=state.get("session_id"),
                                run_id=state.get("run_id"),
                                agent=agent.role.value,
                                success=False,
                                error_code=error.error_code,
                            ),
                            RunEvent(
                                event_type=EventType.RUN_FAILED,
                                sequence=sequence + 2,
                                session_id=state.get("session_id"),
                                run_id=state.get("run_id"),
                                agent=agent.role.value,
                                success=False,
                                error_code=error.error_code,
                            ),
                        ],
                    }
                    if plan is not None:
                        aggregation_failure_updates["task_plan"] = plan.model_copy(
                            update={"status": TaskPlanStatus.FAILED}
                        )
                    return cast(AgentState, aggregation_failure_updates)
                if aggregation_results is not None:
                    plan = _task_plan_from_state(state)
                    if plan is None:
                        raise RuntimeError("aggregation requires a task plan")
                    run_state = cast(
                        AgentState,
                        {
                            **state,
                            "messages": [
                                *state.get("messages", []),
                                _task_results_message(plan, aggregation_results),
                            ],
                        },
                    )

            subagent_traces: list[list[RunEvent]] = []
            parent_state_token = _ACTIVE_PARENT_STATE.set(run_state)
            trace_token = _SUBAGENT_EVENT_TRACES.set(subagent_traces)
            # S5-A1/A2：tool 模式 Supervisor 轮且有 ACTIVE 计划时，注入
            # 轮内执行上下文（ask_* 门控与步骤记账的共享状态）。handoff
            # 模式与无计划的 tool 模式注入 None——工具层行为与既往完全
            # 一致（零回归）。
            tool_plan_holder: _ToolPlanHolder | None = None
            if (
                self.orchestration_mode == "tool"
                and agent.role is AgentRole.SUPERVISOR
            ):
                # 无 ACTIVE 计划也注入空 holder：同轮新建计划时工具需要
                # 一个可写的盒子（见 _ToolPlanHolder 注释）。
                active_plan = _task_plan_from_state(run_state)
                tool_plan_holder = _ToolPlanHolder(
                    _ToolModePlanExecution(
                        active_plan,
                        _task_results_from_state(run_state),
                    )
                    if (
                        active_plan is not None
                        and active_plan.status is TaskPlanStatus.ACTIVE
                    )
                    else None
                )
            # 轮前初始执行对象（用于判定「本轮是否新建了计划」：轮末对象
            # 与它身份不同即说明 create_task_plan_tool_mode 在轮内安装了
            # 新执行上下文，审批早退分支也要据此补发创建事件）。
            initial_tool_execution = (
                tool_plan_holder.execution if tool_plan_holder is not None else None
            )
            tool_plan_token = _TOOL_PLAN_EXECUTION.set(tool_plan_holder)
            # P0-5：学习记录作用域与父状态同位设置（工具层从 scope
            # 读 user_id/session_id，模型不可见不可控）；无条件设置，
            # 无 store 时工具未注册、无人读取，零副作用。
            learning_scope_token = learning_scope.set(
                {
                    "user_id": run_state.get("user_id"),
                    "session_id": run_state.get("session_id"),
                }
            )
            # S5-C1 决策 4：知识空间 scope 与 learning_scope 同位注入——
            # 会话绑定的 namespace 在 run 入口写入 AgentState.extra，此处
            # 读取后供 search_knowledge 工具透传给 service（模型参数面不
            # 暴露空间）。None = 未绑定（检索层按单路 public 过滤处理）。
            knowledge_scope_token = knowledge_scope.set(
                cast(str | None, run_state.get("extra", {}).get("knowledge_namespace"))
            )
            try:
                result = agent.run(run_state)
            finally:
                knowledge_scope.reset(knowledge_scope_token)
                learning_scope.reset(learning_scope_token)
                _SUBAGENT_EVENT_TRACES.reset(trace_token)
                _ACTIVE_PARENT_STATE.reset(parent_state_token)
                _TOOL_PLAN_EXECUTION.reset(tool_plan_token)
            # 轮末重读（而非用 run 前的旧引用）：create_task_plan_tool_mode
            # 可能在同轮新建计划并替换 holder 里的执行对象；holder 为引用
            # 共享，工具线程内的变更在此可见。
            executed_plan = (
                tool_plan_holder.execution if tool_plan_holder is not None else None
            )

            updates = dict(result.updates)
            # ── 注入「产出 Agent 角色」元数据：写入会话历史的唯一闸口 ──
            # _wrap 节点是本次执行所有消息进入 state["messages"]（进而进入
            # checkpointer 持久化）的唯一入口，因此在这里统一给助手消息
            # 打上角色标记，一处覆盖最终回答与带 tool_calls 的中间助手消息。
            #
            # 为什么选这个注入点而不是 ReActAgentNode 内部：
            # 1) ReActAgentNode 是模型边界，其生成的消息会作为下一轮模型
            #    输入；在内部注入会让带角色标记的消息污染模型看到的上下文，
            #    在这里注入则消息仅写入持久化历史——当前 OpenAI 兼容
            #    provider 不会将该键透传给模型 API；若未来接入会透传
            #    additional_kwargs 的 provider，需重新评估该注入点。
            # 2) ReActAgentNode 的单元测试断言 additional_kwargs 精确相等
            #    （test_react_agent.py::test_react_agent_preserves_*），
            #    注入放在图层面既不改变节点语义，也不破坏单元契约。
            # 3) 未来新增图节点只要走 _wrap，就不会漏标角色。
            #
            # 边界情况：
            # - 只处理 AIMessage；HumanMessage（用户输入/任务描述）与
            #   ToolMessage（工具返回）不是助手产出，一律不注入。
            # - 失败轮次（模型调用失败、迭代超限）已产生的助手消息同样
            #   注入，保证历史里每条 AI 消息都有确定的产出者。
            # - 后续聚合逻辑（_replace_terminal_ai_output 等）用 model_copy
            #   仅替换 content，会原样保留 additional_kwargs，因此 Supervisor
            #   聚合的最终回答仍携带 supervisor 角色，不会因改内容而丢失。
            updates["messages"] = [
                with_agent_role(message, agent.role)
                if isinstance(message, AIMessage)
                else message
                for message in cast(list[BaseMessage], updates.get("messages", []))
            ]
            # ── S5-A1：intent 提前求值 + UNCLEAR 违约的簿记作废标志 ──
            # 为什么在这里解析：审批暂停早退分支（下方）位于原 intent 解析
            # 位置之前，而它的计划簿记写回同样必须服从 UNCLEAR 拦截语义。
            # _intent_from_results 是只读纯函数，前移安全；INTENT_DETECTED
            # 事件与 state["intent"] 写入仍留在原位，语义零漂移。
            #
            # 作废条件（三者同时成立才 void 本轮簿记）：
            # 1. 模型自报 UNCLEAR——违背「不明即只追问」约定；
            # 2. Supervisor 轮（tool 模式计划只存在于 Supervisor）；
            # 3. 计划为本轮新建（身份比较）：审批恢复轮从 checkpoint 重建的
            #    存量计划是系统此前认可的进度，void 会制造新的账实分离；
            #    该场景下二次分派已被置空 new_plan 拦截，簿记保留。
            # 不要求「确有分派尝试」：UNCLEAR 下新建计划本身就是违约。
            # tool 模式下该条件与拦截命中（new_plan 非空）等价：handoff
            # 工具未注册使 target 恒为 None，而门控拒绝使恢复轮新建的
            # 计划不会替换 holder 执行对象。
            intent = _intent_from_results(
                cast(list[ToolResult], updates.get("tool_results", []))
            )
            void_plan_bookkeeping = (
                intent is Intent.UNCLEAR
                and agent.role is AgentRole.SUPERVISOR
                and executed_plan is not None
                and executed_plan is not initial_tool_execution
            )
            if updates.get("pending_tool_approval") is not None:
                # The AI tool-call message is now checkpointed.  Route to a
                # separate interrupt node before any terminal-answer or handoff
                # interpretation; resuming that gate cannot replay this model call.
                updates["next_agent"] = None
                updates["pending_handoff"] = None
                updates["run_error"] = None
                # ── S5-A1/A2：审批暂停同样持久化轮内计划簿记（审查 🔴）──
                # 审批中断是计划内调 shell 的正常交互而非异常路径：若在此
                # 直接返回，holder 内的游标推进/步骤结果/事件随 ContextVar
                # reset 全部丢失，恢复后路由器会把已完成的步骤重跑一遍。
                # 因此在这里补做与正常轮末等价的写回；事件直接构造并追加
                # 进本轮 events（序号沿用全局最大序号递增，与 emit 闭包同
                # 一口径——此处位于闭包定义之前，无法复用）。无论计划被
                # 推进到何种状态（ACTIVE/COMPLETED/FAILED）都持久化：
                # 终态计划的结果同样不能丢，且恢复后路由按状态自然分流。
                # 写回条件不依赖 dirty：「本轮新建了计划但尚未执行任何
                # 步骤即暂停」时 dirty 为 False，但计划本身必须入库，
                # 否则恢复后门控与记账静默失效（验收发现的边界缺口）。
                # 已知残余边角：intent 在轮末才从 tool_results 解析，此处
                # UNCLEAR 拦截经上方前移的 void 标志覆盖本分支：
                # 「违约建计划后立即调 shell 暂停」的簿记同样不入库。
                plan_created_this_round = (
                    executed_plan is not None
                    and executed_plan is not initial_tool_execution
                )
                if executed_plan is not None and (
                    executed_plan.dirty or plan_created_this_round
                ) and not void_plan_bookkeeping:
                    updates["task_plan"] = executed_plan.plan
                    updates["task_results"] = executed_plan.results
                    approval_events = cast(
                        list[RunEvent], list(updates.get("events", []))
                    )
                    sequence = max(
                        (
                            event.sequence
                            for event in [
                                *state.get("events", []),
                                *approval_events,
                            ]
                        ),
                        default=-1,
                    )
                    if plan_created_this_round:
                        # 本轮新建了计划（执行对象与轮前不是同一个）：
                        # 补发创建事件，脱敏口径与正常路径一致（只记步骤数）。
                        sequence += 1
                        approval_events.append(
                            RunEvent(
                                event_type=EventType.TASK_PLAN_CREATED,
                                sequence=sequence,
                                session_id=state.get("session_id"),
                                run_id=state.get("run_id"),
                                agent=agent.role.value,
                                content=str(len(executed_plan.plan.steps)),
                            )
                        )
                    for step_result in executed_plan.newly_recorded_results():
                        sequence += 1
                        approval_events.append(
                            RunEvent(
                                event_type=EventType.TASK_RESULT_ARCHIVED,
                                sequence=sequence,
                                session_id=state.get("session_id"),
                                run_id=state.get("run_id"),
                                agent=step_result.target_agent.value,
                                success=step_result.success,
                                error_code=step_result.error_code,
                                plan_step_sequence=step_result.step_sequence,
                            )
                        )
                    updates["events"] = approval_events
                # ── 固定工作流：审批暂停标记（lesson-workflow-design §二）──
                # 与计划簿记同一闸口：工作流 Worker 触发审批时把 status
                # 拨到 PAUSED_APPROVAL（API/前端可见的暂停态）；恢复后
                # Worker 终态经 _workflow_worker_updates 落账并拨回
                # RUNNING。真实冒烟教训：此处不标记则暂停语义只在
                # pending_tool_approval 上可见，工作流状态机脱节。
                paused_workflow = _workflow_from_state(state)
                if (
                    paused_workflow is not None
                    and paused_workflow.status is WorkflowStatus.RUNNING
                ):
                    pause_steps = paused_workflow.steps
                    pause_index = paused_workflow.current_step_index
                    if (
                        pause_index < len(pause_steps)
                        and pause_steps[pause_index].worker_role.value
                        == agent.role.value
                    ):
                        updates["workflow"] = paused_workflow.model_copy(
                            update={"status": WorkflowStatus.PAUSED_APPROVAL}
                        )
                return cast(AgentState, updates)
            tool_results = cast(list[ToolResult], updates.get("tool_results", []))
            # ── S2-T4 结构化引用：把本轮检索命中挂到终端回答 ──
            # 注入时机：与角色元数据同一闸口（_wrap 是消息写入持久化历史
            # 的唯一入口），在角色注入之后、聚合改写之前执行——聚合改写
            # （_replace_terminal_ai_output 等）用 model_copy 保留
            # additional_kwargs，因此引用与角色一样不会因内容改写丢失。
            # 收集时机与来源见 _citations_from_tool_results 注释（与
            # S2-T3 evidence_tool_names 同源：只读本轮新增的工具结果，
            # 按 _CITATION_TOOL_NAMES 过滤检索类工具）。
            # S2-T5：_attach_references 在写入前完成真实性校验与文档级
            # 合并规范化，并返回校验结论（reference_verification），
            # 稍后写入 state["reference_verification"] 并在 evaluator 轮
            # 并入 EvaluationResult（见下方评价组装与 state 写入处注释）。
            updated_messages, reference_verification = _attach_references(
                cast(list[BaseMessage], updates["messages"]),
                tool_results,
            )
            # T5-3：officecli_edit 成功产出的文件清单挂到本轮终端回答，
            # API 层读取后转为可下载附件（与引用同一闸口、同一副本语义，
            # 聚合改写的 model_copy 会原样保留 additional_kwargs）。
            # 来源有两路：审批门写入 state 通道的回执（唯一执行路径），
            # 以及防御性兼容的本轮内联工具结果解析。
            pending_generated = _generated_files_from_state(
                state.get("generated_files")
            )
            updated_messages, generated_attached = _attach_generated_files(
                updated_messages,
                tool_results,
                pending_generated,
            )
            updates["messages"] = updated_messages
            if generated_attached and pending_generated:
                # 挂载后清空通道：同一轮后续 Agent 的回答不重复携带
                updates["generated_files"] = []
            target = _handoff_target(tool_results)  # 本轮模型请求的转交目标
            new_plan = _task_plan_from_results(tool_results)  # 本轮模型新建的计划
            # 固定工作流启动解析（lesson-workflow-design §二）：与计划同一
            # 「工具回 JSON → 轮末解析写回」机制；同轮多次启动只采纳最后
            # 一次（宽容读取，解析失败视为无启动而非崩溃）。
            new_workflow = _workflow_from_results(tool_results)
            existing_plan = _task_plan_from_state(state)  # 已持久化的活动计划
            # ── S2-T1 意图识别：解析模型分类，并对「意图不明」做分派拦截 ──
            # 原理：detect_intent 是 Supervisor 决策前的必备工具（prompt 约定），
            # 其成功结果经 _intent_from_results 校验后成为本轮权威意图。若模型
            # 自报 UNCLEAR（无法确定）却仍试图 handoff 或 create_task_plan，
            # 说明模型违背了「不明即追问」的约定；这里直接把分派动作丢弃
            # （target/new_plan 置 None），让 ReAct 循环继续到模型输出澄清性
            # 回答，从而做到「不随意分派」的运行时硬保障，而不只依赖 prompt。
            #
            # 边界：
            # - 拦截只针对「模型自报 UNCLEAR 仍强行分派」这一种违约；
            #   模型跳过 detect_intent（intent=None）或误报其他意图
            #   （如把备课误报为答疑）属既定的兼容设计，不在此拦截——
            #   前者兼容旧行为与历史替身，后者由 Worker 与评价链路兜底；
            # - 模型在 UNCLEAR 后一直输出工具调用直到迭代超限，会走既有的
            #   REACT_ITERATION_LIMIT 失败路径（fail 分支），不会无限循环；
            # - 兼容旧行为：不调用 detect_intent 的模型（如历史测试替身）拿到
            #   intent=None，不触发拦截，行为与 S2-T1 之前完全一致。
            # S5-A1：UNCLEAR 拦截的作废范围延伸到计划簿记——命中时轮末
            # 写回跳过本轮新建计划的持久化（void 标志已在上方审批分支之前
            # 统一计算），与 handoff 模式「拦截完全作废分派动作」语义对齐；
            # 否则已执行步骤的簿记照常入库，UI 会展示本该被拦截的计划。
            # 已执行的 ask_* 子代理事件仍经 traces 留在审计流中：执行事实
            # 不可撤销，作废的是计划语义而非运行记录。
            if (
                intent is Intent.UNCLEAR
                and agent.role is AgentRole.SUPERVISOR
                and (
                    target is not None
                    or new_plan is not None
                    or new_workflow is not None
                )
            ):
                target = None
                new_plan = None
                # 工作流启动同受 UNCLEAR 拦截：意图不明却启动工作流，
                # 作废启动（产物目录只是空壳，不产生语义副作用）。
                new_workflow = None
            # ── S2-T2 学生水平画像：解析模型识别的水平并写入 state ──
            # 与 intent 的生命周期语义相反（这是本任务的关键设计）：
            # - intent 每轮重置、只属于「本轮」（run() 重置列表含 intent）；
            # - level 是「跨轮保留的学生画像」：本轮未调用 detect_level
            #   时保留 checkpoint 中的旧值（run() 的重置列表刻意不含
            #   level），首次提问无水平信息时为 None，读取侧按
            #   StudentLevel.UNKNOWN 归一（默认中等深度）。
            # 写入不依赖「确定分派」：学生自报水平即使本轮直接回答
            # （无 handoff/计划），也应记录进画像——跨轮画像要为后续
            # 轮次的分层讲解服务，这正是「保留而非重置」的意义。
            level = _level_from_results(tool_results)
            if level is not None:
                updates["level"] = level.value
            # 意图识别结果同步进跨轮持久字段 task_context.intent：
            # Worker 与聚合阶段可读取意图标签做针对性工作，同时保留审计轨迹。
            # 仅在确定分派（非 UNCLEAR、确有目标或计划）时写入，避免「直接回答
            # 澄清问题」这类无任务轮次污染任务上下文。
            # 水平画像在同一处、同一条件同步进 task_context.level（与
            # task_context.intent 同构的快照）：本轮新识别的水平优先，
            # 否则沿用 state 中保留的旧画像——保证分派给 Worker 的任务
            # 上下文始终携带「为哪个水平的学生讲解」，而 state["level"]
            # 仍是权威来源。
            if (
                intent is not None
                and intent is not Intent.UNCLEAR
                and (target is not None or new_plan is not None)
            ):
                current_level = (
                    level.value if level is not None else state.get("level")
                )
                existing_context = state.get("task_context")
                if existing_context is None:
                    updates["task_context"] = TaskContext(
                        intent=intent.value,
                        level=current_level or "",
                    )
                else:
                    context_update: dict[str, str] = {"intent": intent.value}
                    if current_level is not None:
                        context_update["level"] = current_level
                    updates["task_context"] = TaskContext.model_validate(
                        existing_context
                    ).model_copy(update=context_update)
            # 计划取舍：有活动计划则沿用（再新建会触发下方替换拦截），否则采纳本轮新建
            plan = existing_plan or new_plan
            replacing_plan = new_plan is not None and existing_plan is not None
            if new_plan is not None and existing_plan is None:
                updates["task_plan"] = new_plan
                updates["task_results"] = []  # 新计划从零收集子任务结果
            # 固定工作流采纳（lesson-workflow-design §二）：workflow 通道
            # 每用户轮重置，正常轮首恒 None；adopted 标志供事件发射使用。
            existing_workflow = _workflow_from_state(state)
            workflow_adopted = (
                new_workflow is not None and existing_workflow is None
            )
            if workflow_adopted and new_workflow is not None:
                updates["workflow"] = new_workflow
            events = cast(list[RunEvent], updates.get("events", []))
            if subagent_traces:
                events = _interleave_subagent_traces(
                    state,
                    events,
                    subagent_traces,
                )
            sequence = max(
                (
                    event.sequence
                    for event in [*state.get("events", []), *events]
                ),
                default=-1,
            )

            # 事件发射闭包：统一递增序列号并追加进 events，后续分支只需调用
            def emit(
                event_type: EventType,
                event_agent: str,
                *,
                success: bool = True,
                error_code: ErrorCode | None = None,
                plan_step_sequence: int | None = None,
                degraded: bool | None = None,
                event_intent: str | None = None,
                event_verdict: str | None = None,
                # 六大功能 P2-8：GRADING_COMPLETED 事件携带的数字摘要
                # （脱敏：只记题数/总分，正文在 state["grading"]）。
                grading_item_count: int | None = None,
                grading_total_score: float | None = None,
                grading_max_total_score: float | None = None,
                # S4-T3 检索决策：RETRIEVAL_DECISION 事件携带的工具名与
                # 决策摘要字段（语义见 events.py 的 retrieval_* 注释）。
                # 全部默认 None，既有调用方零改动、旧事件不携带。
                event_tool_name: str | None = None,
                # S5-A1：TASK_PLAN_CREATED 事件携带的步骤数（字符串数字，
                # 脱敏——计划正文在 state 通道，事件只记摘要）。
                content: str | None = None,
                retrieval_needed: bool | None = None,
                retrieval_need_reason: str | None = None,
                retrieval_threshold_met: bool | None = None,
                retrieval_stopped_reason: str | None = None,
                retrieval_rounds: int | None = None,
                retrieval_hit_count: int | None = None,
                retrieval_top_score: float | None = None,
                # 固定工作流事件字段（lesson-workflow-design §七）
                workflow_id: str | None = None,
                workflow_step_id: str | None = None,
                workflow_step_index: int | None = None,
                auto_approved: bool | None = None,
            ) -> None:
                nonlocal sequence
                sequence += 1
                events.append(
                    RunEvent(
                        event_type=event_type,
                        sequence=sequence,
                        session_id=state.get("session_id"),
                        run_id=state.get("run_id"),
                        agent=event_agent,
                        success=success,
                        error_code=error_code,
                        plan_step_sequence=plan_step_sequence,
                        degraded=degraded,
                        intent=event_intent,
                        evaluation_verdict=event_verdict,
                        grading_item_count=grading_item_count,
                        grading_total_score=grading_total_score,
                        grading_max_total_score=grading_max_total_score,
                        tool_name=event_tool_name,
                        content=content,
                        retrieval_needed=retrieval_needed,
                        retrieval_need_reason=retrieval_need_reason,
                        retrieval_threshold_met=retrieval_threshold_met,
                        retrieval_stopped_reason=retrieval_stopped_reason,
                        retrieval_rounds=retrieval_rounds,
                        retrieval_hit_count=retrieval_hit_count,
                        retrieval_top_score=retrieval_top_score,
                        workflow_id=workflow_id,
                        workflow_step_id=workflow_step_id,
                        workflow_step_index=workflow_step_index,
                        auto_approved=auto_approved,
                    )
                )

            # ── S5-A1/A2：tool 模式计划事件与执行写回 ──
            # 顺序：先发计划创建事件，再发本轮新落的步骤结果事件，最后
            # 把执行上下文的变化写回 state（plan 局部变量同步更新，使
            # 下游 fail/聚合分支看到推进后的计划状态）。
            if (
                self.orchestration_mode == "tool"
                and new_plan is not None
                and existing_plan is None
            ):
                # 脱敏：事件只记步骤数（content 字符串），计划正文在
                # state["task_plan"] 随 checkpoint 持久化。仅 tool 模式
                # 发：handoff 模式行为零改动（不新增事件，既有测试零回归）。
                emit(
                    EventType.TASK_PLAN_CREATED,
                    agent.role.value,
                    content=str(len(new_plan.steps)),
                )
            if workflow_adopted and new_workflow is not None:
                # 脱敏：事件只记步骤数与注册 id（content/workflow_id），
                # 参数与产物路径在 state["workflow"] 随 checkpoint 持久化。
                emit(
                    EventType.WORKFLOW_STARTED,
                    agent.role.value,
                    content=str(len(new_workflow.steps)),
                    workflow_id=new_workflow.workflow_id,
                )
            if (
                executed_plan is not None
                and executed_plan.dirty
                and not void_plan_bookkeeping
            ):
                # 被 UNCLEAR 拦截作废的本轮计划整块跳过写回：计划与步骤结果
                # 都不入库，下一用户轮由 _new_run_state 自然清空；已执行的
                # ask_* 子代理事件仍经 traces 留在审计流中（执行事实不可撤销，
                # 作废的是计划语义而非运行记录）。此时 TASK_PLAN_CREATED 因
                # new_plan 被置空同样不发，事件流保持自洽（无创建即无归档）。
                updates["task_plan"] = executed_plan.plan
                updates["task_results"] = executed_plan.results
                plan = executed_plan.plan
                for step_result in executed_plan.newly_recorded_results():
                    emit(
                        EventType.TASK_RESULT_ARCHIVED,
                        step_result.target_agent.value,
                        success=step_result.success,
                        error_code=step_result.error_code,
                        plan_step_sequence=step_result.step_sequence,
                    )
                # 本轮内完成全部步骤 → 聚合分支需要拿到完整结果（轮首
                # 预检时计划还是 ACTIVE，aggregation_results 为 None）：
                # 用执行上下文的最终结果作为聚合输入，复用既有的确定性
                # 聚合机制（缺失结果提示 + TASK_RESULTS_AGGREGATED）。
                if (
                    aggregation_results is None
                    and plan.status is TaskPlanStatus.COMPLETED
                ):
                    aggregation_results = executed_plan.results

            # ── S2-T1 意图事件与状态写入 ──
            # 事件是瞬时信号：消费方（api/chat.py 的 EVENT_TYPE_MAP 白名单）对
            # 未映射的新事件类型安全跳过，因此 INTENT_DETECTED 目前只对内部
            # 审计可见（state["events"]），前端流式协议不受影响；后续若要在
            # 前端展示意图，只需在 api 层映射表补一行，无需改 core。
            # state["intent"] 则是持久权威值（见 state.py 字段注释）。
            # 注意两处都写 intent.value（字符串）：state 通道与事件字段都
            # 只存 msgpack 原生类型，避免 checkpoint 对自定义枚举的反序列化
            # 注册依赖；本函数内部的路由判断仍用 Intent 枚举（intent 变量）。
            if intent is not None:
                updates["intent"] = intent.value
                emit(
                    EventType.INTENT_DETECTED,
                    agent.role.value,
                    event_intent=intent.value,
                )

            # ── S4-T3 检索决策事件：把 search_knowledge 元数据转成事件 ──
            # 转换位置为什么在这里（core 侧 _wrap 而非 knowledge 包）：
            # knowledge 包刻意零依赖 core/events.py（零耦合方向见
            # retrieval.py 模块注释第 8 节第 4 点）——工具结果里的
            # metadata 是纯 JSON 结构，由本文件解析成 RunEvent 追加进
            # events 通道（随 checkpoint 持久化，供评价 Agent 与审计
            # 链路读取「检索是否达标、为何停止」）。
            # 脱敏：事件只记决策摘要，不记查询正文（正文已在工具调用
            # 参数与 tool_results 审计中）——与 evaluation 事件「只记
            # 结论摘要」同一原则。每个 search_knowledge 成功结果发一个
            # 事件（agent=当前角色、tool_name="search_knowledge"），
            # 序列号由 emit 闭包统一递增，与既有事件顺序自洽。
            # 未启用自适应（工具输出无 metadata）→ 解析结果为空，
            # 不发任何事件——「默认零回归、无新事件」由此保证。
            for decision in _retrieval_decisions_from_results(tool_results):
                emit(
                    EventType.RETRIEVAL_DECISION,
                    agent.role.value,
                    event_tool_name="search_knowledge",
                    retrieval_needed=decision["needed"],
                    retrieval_need_reason=decision["need_reason"],
                    retrieval_threshold_met=decision["threshold_met"],
                    retrieval_stopped_reason=decision["stopped_reason"],
                    retrieval_rounds=decision["rounds"],
                    retrieval_hit_count=decision["hit_count"],
                    retrieval_top_score=decision["top_score"],
                )

            # ── S2-T3 评价结论：解析 submit_evaluation 结果并写入 state/事件 ──
            # 触发时机设计：评价作为 evaluator 的 ReAct 轮内动作（prompt +
            # 工具约定，与 detect_intent/detect_level 同构），在「最终回答
            # 产出后」（evaluator 轮结束时）由 _wrap 统一解析——不新增图
            # 节点/路由，不改聚合轮，事件协议向后兼容（EVENT_TYPE_MAP 对
            # 未知事件安全跳过）。模型先观察检索证据（ToolMessage），再
            # 调用 submit_evaluation，最后给出评价文本，_wrap 在轮末把
            # 结论写入 state["evaluation"]。
            #
            # 评价输入如何组装（「不凭空评价」的确定性保障）：
            # - 最终回答：在模型可见的消息历史中（ReAct 输入），无需额外注入；
            # - 本轮检索证据：以 ToolResult 进入模型上下文（工具观察），
            #   _wrap 解析时把「本轮 evaluator 成功执行、有输出、且不是
            #   submit_evaluation 本身」的工具名组装进
            #   EvaluationResult.evidence_tool_names——只记工具名不记正文
            #   （正文仍在 state["tool_results"] 按工具结果审计），既给审计
            #   留了「评价基于哪些证据」的核对线索，又不把证据正文复制进
            #   评价模型造成双重存储。
            # 未调用 submit_evaluation（旧行为/历史替身/模型违约）→
            # evaluation 保持 None、不发事件，运行不受影响。
            # 角色守卫（纯防御性）：只允许 evaluator 轮的 submit_evaluation
            # 结果进入解析与写入。当前权限模型下（注册时 allowed_roles 仅
            # evaluator）成功记录只可能来自 evaluator，其他角色的调用必然
            # TOOL_UNAUTHORIZED 失败；守卫保证未来权限调整也不会让非评价
            # 角色写入评价结论。
            evaluation_input = (
                _evaluation_from_results(tool_results)
                if agent.role is AgentRole.EVALUATOR
                else None
            )
            if evaluation_input is not None:
                # 证据工具名：本轮成功且有输出的工具结果，排除评价工具本身；
                # 保持出现顺序去重（dict.fromkeys），保证审计列表稳定可读。
                # 骨架期取舍（S2-T4 确认后的决定）：范围偏宽——凡成功有
                # 输出的业务工具都算「证据」（可能含非检索类业务工具），
                # 不区分类型；S2-T3 预留的「按证据类型（Citation/检索类
                # 工具）过滤」未落地，原因有二：1) 检索类工具
                # （search_knowledge）天然满足「成功且有输出」，已被纳入
                # 评价证据，行为无需改变；2) 保持向后兼容、不破坏 S2-T3
                # 已交付行为（既有测试断言证据名含替身检索工具 retrieve）。
                # 引用的结构化校验由 S2-T4 的 references 元数据承担
                # （终端回答 additional_kwargs["references"]，读取入口
                # message_references），评价侧继续只记工具名、不记正文。
                evidence_tools = list(
                    dict.fromkeys(
                        result.tool_name
                        for result in tool_results
                        if result.success
                        and result.output
                        and result.tool_name != "submit_evaluation"
                    )
                )
                # 边界语义（与 intent 写入模式一致）：若 evaluator 本轮先成功
                # 提交了 submit_evaluation、随后模型迭代超限或调用失败，会
                # 出现「state["evaluation"] 有结论 + RUN_FAILED 事件」共存——
                # 评价结论本身完整（工具已成功执行），失败发生在评价产出之后，
                # 两者是先后关系而非矛盾，审计者按事件序列读取即可。intent/
                # level 同此语义（成功工具结果先写、轮末失败再补 RUN_FAILED）。
                # 设计边界：「不凭空评价」目前无运行时硬校验——模型在零证据
                # （无任何检索工具调用）下提交 pass 不会被拦截。骨架期以
                # prompt 约定（禁止凭空评价）+ 审计闭环（evidence_tool_names
                # 记录本轮实际证据，审计者可见「零证据却判通过」的可疑评价）
                # 实现；S2-T5 做引用真实性校验时再考虑运行时加强。
                # S2-T5 评价联动：把本轮引用校验结论并入评价结果——
                # 验收要求校验结论「在评价结果中体现」，读取方拿到
                # evaluation 即可同时看到引用校验结论（剔除/合并计数
                # 与明细），与 evidence_tool_names 的「证据由核心层
                # 组装」同一哲学；无校验内容（无引用无剔除）时为 None，
                # 向后兼容旧数据。
                #
                # 取值口径（I-1 修复，真正实现「被评价轮」的联动）：
                # 优先用 evaluator 轮自身结论（reference_verification——
                # 本轮检索/剔除的校验结果）；evaluator 只评价不检索时
                # 本轮结论为 None，则回退到 state 中已写入的「本用户轮」
                # 结论——典型场景：计划流程中 worker 轮先检索作答（伪造
                # 被剔除、结论写入 state["reference_verification"]），
                # evaluator 轮随后评价，评价结果必须携带 worker 轮的
                # 剔除明细（removed/chunk_id），「伪造剔除在评价结果中
                # 体现」才算成立。
                # state 读取路径核实：_wrap 的 state 参数是「本轮执行前
                # 的 state」，包含同一用户轮内先前 agent 轮已写入
                # checkpoint 的 updates（langgraph 节点链式传递）；run()
                # 每轮重置 reference_verification 为 None，不存在跨轮
                # 污染（旧轮结论不会误入本轮评价）。宽容读取：checkpoint
                # 反序列化后通道值可能是 dict 或 ReferenceVerification
                # 实例（视序列化器而定），统一归一为模型再赋值。
                # 取舍（与 reviewer 方案 a 一致）：若 evaluator 轮自身
                # 也检索（新结论非 None），以 evaluator 轮结论为准——
                # 评价结果内嵌「评价所依据轮次」的校验结论，worker 轮
                # 明细仍保留在 state["reference_verification"] 被覆盖
                # 前的审计路径（本轮 state 最终值为 evaluator 轮结论），
                # 简单一致、不做两轮并集。
                evaluation_verification: Any = reference_verification or cast(
                    Any, state.get("reference_verification")
                )
                # 宽容归一化：checkpoint 反序列化后通道值可能是 dict 或
                # ReferenceVerification 实例（视序列化器而定），统一归一
                # 为模型再赋值（cast(Any) 让本分支在静态类型下可达，同时
                # 运行时防御 dict 形态——与仓库「读取端宽容」哲学一致）。
                if evaluation_verification is not None and not isinstance(
                    evaluation_verification, ReferenceVerification
                ):
                    evaluation_verification = ReferenceVerification.model_validate(
                        evaluation_verification
                    )
                updates["evaluation"] = EvaluationResult(
                    verdict=EvaluationVerdict(evaluation_input["verdict"]),
                    fact_accuracy=EvaluationVerdict(
                        evaluation_input["fact_accuracy"]
                    ),
                    citation_completeness=EvaluationVerdict(
                        evaluation_input["citation_completeness"]
                    ),
                    reason=evaluation_input["reason"],
                    evidence_tool_names=evidence_tools,
                    reference_verification=evaluation_verification,
                )
                # 事件脱敏：只发 verdict 摘要（无敏感正文），完整结论
                # （含 reason 与引用校验结论）在 state["evaluation"]
                # 随 checkpoint 持久化。
                emit(
                    EventType.EVALUATION_COMPLETED,
                    agent.role.value,
                    event_verdict=evaluation_input["verdict"],
                )

            # ── P2-10 批改结论：通道写入 + 消息挂载 + 确定性落库 + 事件 ──
            # 双来源（角色守卫同 evaluation 模式）：
            # - handoff 模式：evaluator 轮自身 tool_results 里的
            #   submit_grading 结果；
            # - tool 模式（生产）：Supervisor 轮从 ask_evaluator 输出
            #   提取（_run_subagent 已把子代理批改负载与幂等键放进
            #   grading 键——子代理不经 _wrap，这是生产可见的唯一通道）。
            # 落库不靠模型自觉（pi 审查 🔴3）：解析成功即逐题确定性
            # 落库；store 未注入时静默跳过（None 容忍守卫在落库函数内）。
            grading_pair: tuple[GradingResult, str] | None = None
            if agent.role is AgentRole.EVALUATOR:
                grading_pair = _grading_from_results(tool_results)
            elif agent.role is AgentRole.SUPERVISOR:
                grading_pair = _grading_from_supervisor_results(tool_results)
            if grading_pair is not None:
                grading_result, grading_tool_call_id = grading_pair
                updates["grading"] = grading_result
                # 消息元数据回放（pi 审查 🟡4）：挂到本轮终端回答，
                # 任意历史轮的批改卡刷新后经 history 端点恢复。
                updates["messages"] = _attach_grading(
                    cast(list[BaseMessage], updates["messages"]),
                    grading_result,
                )
                _persist_grading_records(
                    self._learning_store,
                    grading_result,
                    state.get("user_id"),
                    state.get("session_id"),
                    grading_tool_call_id,
                )
                # 事件脱敏：只发数字摘要（pi 审查 🟡C），逐题反馈等
                # 正文在 state["grading"] 与消息元数据随 checkpoint 持久化。
                emit(
                    EventType.GRADING_COMPLETED,
                    agent.role.value,
                    grading_item_count=len(grading_result.items),
                    grading_total_score=grading_result.total_score,
                    grading_max_total_score=grading_result.max_total_score,
                )

            # ── S2-T5 引用真实性校验结论写入 state ──
            # 权威来源（供审计与 API 层读取，随 checkpoint 持久化）。
            # 与 evaluation.reference_verification 的关系：两者**并不
            # 恒等**——本通道记录「本轮实际校验动作」（本轮挂载/剔除），
            # evaluator 轮的评价结果在自身无校验内容时回退并入「本用户
            # 轮先前轮次」的结论（见上方评价组装注释），因此 evaluation
            # 内嵌的可能是先前 worker 轮的结论而 state 通道仍是本轮的。
            # 只有本轮确实产生了校验内容（挂载了引用或剔除了伪造）才写入，
            # 全零结论（无检索无引用）不写、保持 None——与 evaluation 的
            # 「无评价 → None」同一语义，避免 checkpoint 出现噪音字段。
            # 脱敏：结论只含计数与 chunk_id/document_id 结构化标识，
            # 不复制引用正文（正文仍按 tool_results 审计）。
            if reference_verification is not None:
                updates["reference_verification"] = reference_verification

            # 本轮默认收口值：不转交、无错误；后续分支按需覆盖
            handoff_count = state.get("handoff_count", 0)
            switch_count = state.get("agent_switch_count", 0)
            updates["next_agent"] = None
            updates["run_error"] = None

            # 失败收口闭包：写 run_error、把活动计划标 FAILED、补发失败事件
            def fail(error: RunError) -> AgentState:
                updates["run_error"] = error
                if plan is not None and plan.status not in {
                    TaskPlanStatus.CANCELLED,
                    TaskPlanStatus.FAILED,
                }:
                    updates["task_plan"] = plan.model_copy(
                        update={"status": TaskPlanStatus.FAILED}
                    )
                if aggregation_results is not None:
                    emit(
                        EventType.TASK_RESULTS_AGGREGATED,
                        AgentRole.SUPERVISOR.value,
                        success=False,
                        error_code=error.error_code,
                        degraded=any(
                            not item.success for item in aggregation_results
                        ),
                    )
                emit(
                    EventType.RUN_FAILED,
                    agent.role.value,
                    success=False,
                    error_code=error.error_code,
                )
                updates["handoff_count"] = handoff_count
                updates["agent_switch_count"] = switch_count
                updates["events"] = events
                return cast(AgentState, updates)

            # 计划 Worker 的模型调用失败/迭代超限视为可重试：不判死，由调度节点再分派
            planned_worker = (
                agent.role is not AgentRole.SUPERVISOR
                and plan is not None
                and plan.status is TaskPlanStatus.ACTIVE
            )
            # 固定工作流当前步骤 Worker 同样豁免：错误交工作流簿记落
            # FAILED → 调度节点执行 on_failure 策略。稳定性冒烟 2026-08-30
            # 根因：工作流 Worker 迭代超限在这道闸被提前判死，步骤永远
            # 停在 RUNNING，on_failure（retry/continue）从未执行——
            # 4 条教案冒烟 3 条冻结在 review（events 以
            # react_iteration_limit 收尾）。
            workflow_step_worker = self._workflow_current_step_worker(
                state, agent.role
            )
            recoverable_planned_error = (
                planned_worker
                and result.error is not None
                and result.error.error_code
                in {
                    ErrorCode.MODEL_CALL_FAILED,
                    ErrorCode.REACT_ITERATION_LIMIT,
                }
            ) or (
                workflow_step_worker
                and result.error is not None
                and result.error.error_code
                in {
                    ErrorCode.MODEL_CALL_FAILED,
                    ErrorCode.REACT_ITERATION_LIMIT,
                }
            )
            if result.error is not None and not recoverable_planned_error:
                return fail(result.error)
            if replacing_plan:
                return fail(
                    RunError(
                        error_code=ErrorCode.GRAPH_INVALID_TARGET,
                        message="当前用户轮次已存在任务计划，不允许覆盖",
                        agent=agent.role.value,
                    )
                )
            if target is not None and target not in registered_targets:
                return fail(
                    RunError(
                        error_code=ErrorCode.GRAPH_INVALID_TARGET,
                        message=f"非法 next_agent：{target}",
                        agent=agent.role.value,
                    )
                )

            if target is not None and plan is not None:
                if plan.status is not TaskPlanStatus.ACTIVE:
                    return fail(
                        RunError(
                            error_code=ErrorCode.GRAPH_INVALID_TARGET,
                            message="任务计划结束后不允许继续 handoff",
                            agent=agent.role.value,
                        )
                    )
                expected_target = plan.steps[
                    plan.current_step_index
                ].target_agent.value
                if target != expected_target:
                    return fail(
                        RunError(
                            error_code=ErrorCode.GRAPH_INVALID_TARGET,
                            message=f"handoff 目标偏离当前计划步骤：{target}",
                            agent=agent.role.value,
                        )
                    )
                # 活动计划由确定性调度节点分派；一致的模型 handoff 仅作冗余观察。
                target = None

            if agent.role is AgentRole.SUPERVISOR:
                if target is not None:
                    if handoff_count + 1 > self.max_handoffs:
                        error = RunError(
                            error_code=ErrorCode.GRAPH_HANDOFF_LIMIT,
                            message=f"handoff 次数超过上限：{self.max_handoffs}",
                            agent=agent.role.value,
                        )
                        return fail(error)
                    if switch_count + 1 > self.max_agent_switches:
                        return fail(
                            RunError(
                                error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                                message=(
                                    "Agent 切换次数超过上限："
                                    f"{self.max_agent_switches}"
                                ),
                                agent=agent.role.value,
                            )
                        )
                    updates["next_agent"] = target
                    if self.interrupt_before_handoff:
                        updates["pending_handoff"] = HandoffApprovalRequest(
                            target_agent=AgentRole(target),
                            task_content=_latest_human_content(
                                state.get("messages", [])
                            ),
                        )
                    else:
                        handoff_count += 1
                        switch_count += 1
                        emit(EventType.AGENT_SWITCHED, target)
                elif (
                    plan is None
                    or plan.status is not TaskPlanStatus.ACTIVE
                    # S5-A1：tool 模式 Supervisor 轮结束即运行收口（活动
                    # 计划不走分派节点，见 _route 注释），因此也要发
                    # RUN_COMPLETED——否则「建计划后模型直接作答」的轮次
                    # 无收口事件。聚合块内 aggregation_results 对 ACTIVE
                    # 计划恒为 None，不会误触发聚合。
                    or self.orchestration_mode == "tool"
                ):
                    if aggregation_results is not None:
                        if plan is None:
                            raise RuntimeError("aggregation requires a task plan")
                        generated = cast(
                            list[BaseMessage],
                            updates.get("messages", []),
                        )
                        fallback_used = _terminal_agent_output(generated) is None
                        if fallback_used:
                            updates["messages"] = _replace_terminal_ai_output(
                                generated,
                                _deterministic_aggregation(
                                    plan,
                                    aggregation_results,
                                ),
                            )
                            events = _mark_agent_completion_invalid(
                                events,
                                AgentRole.SUPERVISOR,
                            )
                        has_missing_results = any(
                            not item.success for item in aggregation_results
                        )
                        if has_missing_results and not fallback_used:
                            updates["messages"] = _append_missing_results_notice(
                                generated,
                                plan,
                                aggregation_results,
                            )
                        emit(
                            EventType.TASK_RESULTS_AGGREGATED,
                            agent.role.value,
                            degraded=has_missing_results or fallback_used,
                        )
                    emit(EventType.RUN_COMPLETED, agent.role.value)
            else:
                # ── 固定工作流 Worker 轮簿记（lesson-workflow-design §二）──
                # 先于计划簿记：工作流运行中且本 Worker 是当前步骤目标时，
                # 终态直接落步骤状态并提前返回（路由交 _route 的 workflow
                # 分支 → 调度节点），不走计划/切换计数——工作流预算自成
                # 体系（lesson-workflow-design §八）。审批暂停只翻工作流
                # 状态、步骤保持 RUNNING；恢复后 Worker 携批准结果重跑。
                workflow_updates = self._workflow_worker_updates(
                    state,
                    agent,
                    result,
                    emit,
                )
                if workflow_updates is not None:
                    updates.update(workflow_updates)
                    updates["events"] = events
                    updates["handoff_count"] = handoff_count
                    updates["agent_switch_count"] = switch_count
                    return cast(AgentState, updates)
                # Worker 轮：按活动计划记录本步骤结果并推进游标，完成后交回 Supervisor
                if plan is not None and plan.status is TaskPlanStatus.ACTIVE:
                    step = plan.steps[plan.current_step_index]
                    if step.target_agent is not agent.role:
                        return fail(
                            RunError(
                                error_code=ErrorCode.GRAPH_INVALID_TARGET,
                                message=(
                                    "当前 Worker 与计划步骤目标不一致："
                                    f"{agent.role.value}"
                                ),
                                agent=agent.role.value,
                            )
                        )
                    try:
                        existing_results = _task_results_from_state(state)
                        _validate_task_result_prefix(plan, existing_results)
                    except ValueError:
                        return fail(
                            RunError(
                                error_code=ErrorCode.GRAPH_AGGREGATION_INVALID,
                                message="已有任务结果与计划游标不一致",
                                agent=agent.role.value,
                            )
                        )
                    output = (
                        None
                        if result.error is not None
                        else _terminal_agent_output(result.messages)
                    )
                    result_error_code = (
                        result.error.error_code
                        if result.error is not None
                        else None
                    )
                    if result_error_code is None and output is None:
                        result_error_code = ErrorCode.AGENT_OUTPUT_INVALID
                        events = _mark_agent_completion_invalid(
                            events,
                            agent.role,
                        )
                    step_result = TaskStepResult(
                        step_sequence=step.sequence,
                        target_agent=step.target_agent,
                        success=result_error_code is None,
                        output=output,
                        error_code=result_error_code,
                    )
                    task_results = [*existing_results, step_result]
                    next_index = plan.current_step_index + 1
                    next_status = (
                        TaskPlanStatus.COMPLETED
                        if next_index == len(plan.steps)
                        else TaskPlanStatus.ACTIVE
                    )
                    plan = plan.model_copy(
                        update={
                            "current_step_index": next_index,
                            "status": next_status,
                        }
                    )
                    updates["task_plan"] = plan
                    updates["task_results"] = task_results
                    emit(
                        EventType.TASK_RESULT_ARCHIVED,
                        agent.role.value,
                        success=step_result.success,
                        error_code=step_result.error_code,
                        plan_step_sequence=step.sequence,
                    )
                if switch_count + 1 > self.max_agent_switches:
                    return fail(
                        RunError(
                            error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                            message=(
                                "Agent 切换次数超过上限："
                                f"{self.max_agent_switches}"
                            ),
                            agent=agent.role.value,
                        )
                    )
                switch_count += 1
                emit(EventType.AGENT_SWITCHED, AgentRole.SUPERVISOR.value)

            updates["handoff_count"] = handoff_count
            updates["agent_switch_count"] = switch_count
            updates["events"] = events
            return cast(AgentState, updates)

        return RunnableLambda(node)

    def _dispatch_task_plan(self, state: AgentState) -> AgentState:
        """按持久化计划选择下一 Worker，不把顺序控制交还给模型。"""
        plan = _task_plan_from_state(state)
        if plan is None or plan.status is not TaskPlanStatus.ACTIVE:
            raise RuntimeError("task plan dispatch requires an active plan")
        step = plan.steps[plan.current_step_index]
        handoff_count = state.get("handoff_count", 0)
        switch_count = state.get("agent_switch_count", 0)
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )

        # 分派前先查转交/切换上限：超限直接以失败收口，不把超限轮交给 Worker
        limit_error: RunError | None = None
        if handoff_count + 1 > self.max_handoffs:
            limit_error = RunError(
                error_code=ErrorCode.GRAPH_HANDOFF_LIMIT,
                message=f"handoff 次数超过上限：{self.max_handoffs}",
                agent=AgentRole.SUPERVISOR.value,
            )
        elif switch_count + 1 > self.max_agent_switches:
            limit_error = RunError(
                error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                message=f"Agent 切换次数超过上限：{self.max_agent_switches}",
                agent=AgentRole.SUPERVISOR.value,
            )
        if limit_error is not None:
            return cast(
                AgentState,
                {
                    "current_agent": AgentRole.SUPERVISOR.value,
                    "next_agent": None,
                    "pending_handoff": None,
                    "task_plan": plan.model_copy(
                        update={"status": TaskPlanStatus.FAILED}
                    ),
                    "run_error": limit_error,
                    "handoff_count": handoff_count,
                    "agent_switch_count": switch_count,
                    "events": [
                        RunEvent(
                            event_type=EventType.RUN_FAILED,
                            sequence=sequence + 1,
                            session_id=state.get("session_id"),
                            run_id=state.get("run_id"),
                            agent=AgentRole.SUPERVISOR.value,
                            success=False,
                            error_code=limit_error.error_code,
                        )
                    ],
                },
            )

        # 正常分派：确定下一 Worker；审批开启时挂起等人确认，否则直接注入描述
        updates: dict[str, object] = {
            "current_agent": AgentRole.SUPERVISOR.value,
            "next_agent": step.target_agent.value,
            "pending_handoff": None,
            "task_plan": plan,
            "run_error": None,
            "handoff_count": handoff_count,
            "agent_switch_count": switch_count,
        }
        if self.interrupt_before_handoff:
            updates["pending_handoff"] = HandoffApprovalRequest(
                target_agent=step.target_agent,
                task_content=step.description,
                plan_step_sequence=step.sequence,
            )
        else:
            updates["messages"] = [HumanMessage(content=step.description)]
            updates["handoff_count"] = handoff_count + 1
            updates["agent_switch_count"] = switch_count + 1
            updates["events"] = [
                RunEvent(
                    event_type=EventType.AGENT_SWITCHED,
                    sequence=sequence + 1,
                    session_id=state.get("session_id"),
                    run_id=state.get("run_id"),
                    agent=step.target_agent.value,
                    success=True,
                )
            ]
        return cast(AgentState, updates)

    @staticmethod
    def _workflow_current_step_worker(state: AgentState, role: AgentRole) -> bool:
        """工作流运行中且当前步骤的目标角色就是该角色。

        供 Worker 轮错误处置分流用：命中时模型调用失败/迭代超限不判死
        整轮，交 _workflow_worker_updates 落步骤 FAILED，由调度节点按
        on_failure 策略处置（与 _workflow_worker_updates 的合法入口
        条件保持一致：RUNNING / PAUSED_APPROVAL）。
        """
        workflow = _workflow_from_state(state)
        if workflow is None or workflow.status not in {
            WorkflowStatus.RUNNING,
            WorkflowStatus.PAUSED_APPROVAL,
        }:
            return False
        index = workflow.current_step_index
        if index >= len(workflow.steps):
            return False
        return workflow.steps[index].worker_role == role

    def _workflow_worker_updates(
        self,
        state: AgentState,
        agent: ReActAgentNode,
        result: ReActResult,
        emit: Callable[..., None],
    ) -> dict[str, Any] | None:
        """工作流 Worker 轮终态 → 步骤状态更新（lesson-workflow-design §二）。

        返回 None 表示本轮不归工作流管（无工作流 / 终态 / 角色与当前
        步骤不符——后者交回既有逻辑兜底）。步骤 attempts 在分派时由
        调度节点递增，这里只落终态与有界摘要；重试/回退决策全部在
        调度节点（_workflow_dispatch），保持「记录」与「决策」分离。
        """
        workflow = _workflow_from_state(state)
        # RUNNING 之外的合法入口：PAUSED_APPROVAL——审批恢复后 Worker 携
        # 批准结果重跑，终态在这里落账并把工作流拨回 RUNNING（真实冒烟
        # 发现：只认 RUNNING 会让恢复后的簿记被跳过，步骤卡 RUNNING、
        # 调度节点防御性 raise、整轮图异常终止）。
        if workflow is None or workflow.status not in {
            WorkflowStatus.RUNNING,
            WorkflowStatus.PAUSED_APPROVAL,
        }:
            return None
        resumed_from_pause = workflow.status is WorkflowStatus.PAUSED_APPROVAL
        steps = list(workflow.steps)
        index = workflow.current_step_index
        if index >= len(steps):
            return None
        step = steps[index]
        if step.worker_role.value != agent.role.value:
            return None
        if result.updates.get("pending_tool_approval") is not None:
            # 产物区外写操作触发审批门：步骤保持 RUNNING，工作流进入
            # 暂停态供 API/前端展示；批准恢复后 Worker 携工具结果重跑
            # 并最终落终态（见 _approve_tool 的 next_agent 回指）。
            return {
                "workflow": workflow.model_copy(
                    update={"status": WorkflowStatus.PAUSED_APPROVAL}
                ),
            }
        output = (
            None if result.error is not None else _terminal_agent_output(
                result.messages
            )
        )
        failure_code = (
            result.error.error_code if result.error is not None else None
        )
        if failure_code is None and output is None:
            failure_code = ErrorCode.AGENT_OUTPUT_INVALID
        # ── 结构门禁（ppt-workflow-design §五-1）──
        # 声明 output_validator 的步骤：终端输出未通过结构校验 → 按
        # AGENT_OUTPUT_INVALID 判 FAILED，不进暂存、由 on_failure 处置
        # （outline 坏 JSON 不能等导出工具读到垃圾才失败）。
        step_definition: WorkflowStepDefinition | None = None
        workflow_definition = get_workflow(workflow.workflow_id)
        if workflow_definition is not None and index < len(
            workflow_definition.steps
        ):
            step_definition = workflow_definition.steps[index]
        if (
            failure_code is None
            and step_definition is not None
            and step_definition.output_validator is not None
            and output is not None
            and not step_definition.output_validator(output)
        ):
            failure_code = ErrorCode.AGENT_OUTPUT_INVALID
        # ── 产物落盘闸（ppt-workflow-design §五-2）──
        # 声明 requires_artifact 的步骤：磁盘上存在「期望文件名且非空」
        # 才允许 COMPLETED——不信任模型输出与回执登记，防谎报完成。
        if (
            failure_code is None
            and step_definition is not None
            and step_definition.requires_artifact
        ):
            expected = _expected_artifact_path(workflow, step_definition)
            if (
                expected is None
                or not expected.is_file()
                or expected.stat().st_size == 0
            ):
                failure_code = ErrorCode.AGENT_OUTPUT_INVALID
        updated_step = step.model_copy(
            update={
                "status": (
                    WorkflowStepStatus.COMPLETED
                    if failure_code is None
                    else WorkflowStepStatus.FAILED
                ),
                "summary": _bounded_workflow_summary(output, failure_code),
            }
        )
        # 产物暂存（lesson-workflow-design 2026-08-29 探索结论）：成功
        # 步骤的终端输出按 step_id 存入 step_outputs——draft 的教案全文
        # 由此交给确定性导出工具，模型不通过 CLI 参数搬运长正文。
        new_outputs = dict(workflow.step_outputs)
        if failure_code is None and output:
            new_outputs[step.step_id] = output[:60_000]
        # 产物登记（lesson-workflow-design §五）：本步 officecli_edit 生成
        # 的文件回执 → workflow.artifacts（相对 artifact_root 的 POSIX 相
        # 对路径）。产物根外的写操作须人工审批（不经此处），故此处仅
        # 登记产物区内文件；越界文件跳过（宽容读取）。
        new_artifacts = list(workflow.artifacts)
        if workflow.artifact_root:
            for generated in _generated_files_from_tool_results(
                cast(
                    Sequence[ToolResult],
                    result.updates.get("tool_results", []),
                )
            ):
                try:
                    relative = (
                        Path(generated.path)
                        .resolve()
                        .relative_to(Path(workflow.artifact_root).resolve())
                        .as_posix()
                    )
                except ValueError:
                    continue
                if relative not in new_artifacts:
                    new_artifacts.append(relative)
        # 导出工具回执登记（ppt-workflow-design §十六评审补充-1）：
        # export_workflow_docx / export_workflow_pptx 在 core 侧直接调用
        # officecli，产物不经模型 tool_results 的 generated_files 通道——
        # 这里从导出回执解析产物路径并显式登记（两工作流统一收尾）。
        for export_result in cast(
            Sequence[ToolResult], result.updates.get("tool_results", [])
        ):
            if export_result.tool_name not in {
                "export_workflow_docx",
                "export_workflow_pptx",
            } or not export_result.success:
                continue
            try:
                receipt = json.loads(export_result.output)
            except ValueError:
                continue
            produced = (
                receipt.get("pptx") or receipt.get("docx")
                if isinstance(receipt, dict)
                else None
            )
            if not produced or not workflow.artifact_root:
                continue
            try:
                relative = (
                    Path(str(produced))
                    .resolve()
                    .relative_to(Path(workflow.artifact_root).resolve())
                    .as_posix()
                )
            except (ValueError, OSError):
                continue
            if relative not in new_artifacts:
                new_artifacts.append(relative)
        emit(
            EventType.WORKFLOW_STEP_COMPLETED,
            agent.role.value,
            success=failure_code is None,
            error_code=failure_code,
            workflow_id=workflow.workflow_id,
            workflow_step_id=step.step_id,
            workflow_step_index=index + 1,
        )
        return {
            "workflow": workflow.model_copy(
                update={
                    "steps": [
                        *steps[:index],
                        updated_step,
                        *steps[index + 1 :],
                    ],
                    "artifacts": new_artifacts,
                    "step_outputs": new_outputs,
                    # 审批恢复后的终态落账同时把工作流拨回 RUNNING：
                    # 调度节点的失败策略/回退判定以 RUNNING 为前提。
                    "status": (
                        WorkflowStatus.RUNNING if resumed_from_pause else workflow.status
                    ),
                }
            ),
        }

    def _workflow_dispatch(self, state: AgentState) -> AgentState:
        """固定工作流确定性调度：按 current_step_index 分派下一个 Worker。

        决策全部来自注册表定义（步骤顺序/指令模板/预算/失败策略），
        模型零参与。终态步骤的失败策略与 revise 回退在此统一处置；
        分派总是产出 next_agent（Worker 或收口 Supervisor），不会自环。
        """
        workflow = _workflow_from_state(state)
        if workflow is None:
            raise RuntimeError("workflow dispatch requires a workflow state")
        definition = get_workflow(workflow.workflow_id)
        if definition is None:
            raise RuntimeError(
                f"workflow definition missing: {workflow.workflow_id}"
            )
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )
        emitted: list[RunEvent] = []

        def emit_local(event_type: EventType, agent_value: str, **values: Any) -> None:
            nonlocal sequence
            sequence += 1
            emitted.append(
                RunEvent(
                    event_type=event_type,
                    sequence=sequence,
                    session_id=state.get("session_id"),
                    run_id=state.get("run_id"),
                    agent=agent_value,
                    **values,
                )
            )

        steps = list(workflow.steps)
        index = workflow.current_step_index

        # 预算守卫：每步每轮至多分派 2 次（首发 + 重试 1），轮数上限 =
        # max_revise_rounds + 1；超界说明调度缺陷而非模型行为，熔断为
        # WORKFLOW_BUDGET_EXCEEDED。
        attempt_budget = len(steps) * 2 * (definition.max_revise_rounds + 1)
        if sum(step.attempts for step in steps) > attempt_budget:
            return self._workflow_failed_updates(
                state,
                workflow,
                definition,
                ErrorCode.WORKFLOW_BUDGET_EXCEEDED,
                sequence,
            )

        # 阶段 1：刚完成步骤的 revise 回退检查（COMPLETED 且策略命中时，
        # 将 [fallback, index] 重置 PENDING 并计一轮 revise）。
        if index < len(steps):
            step = steps[index]
            if (
                step.status is WorkflowStepStatus.COMPLETED
                and definition.revise_policy is not None
                and workflow.attempts < definition.max_revise_rounds
            ):
                fallback_index = definition.revise_policy(
                    index,
                    step.summary,
                )
                if fallback_index is not None and 0 <= fallback_index < index:
                    for reset in range(fallback_index, index + 1):
                        steps[reset] = steps[reset].model_copy(
                            update={
                                "status": WorkflowStepStatus.PENDING,
                                # 回退轮重新计预算：重做的成稿/生成步骤
                                # 保留完整首发+重试额度（总量由上方
                                # attempt_budget 守卫兜底）。
                                "attempts": 0,
                            }
                        )
                    workflow = workflow.model_copy(
                        update={
                            "steps": steps,
                            "attempts": workflow.attempts + 1,
                        }
                    )
                    emit_local(
                        EventType.WORKFLOW_STEP_RETRY,
                        AgentRole.SUPERVISOR.value,
                        workflow_id=workflow.workflow_id,
                        workflow_step_id=steps[fallback_index].step_id,
                        workflow_step_index=fallback_index + 1,
                    )
                    index = fallback_index

        # 阶段 2：失败策略处置（每轮至多一个 FAILED，处理后的推进由
        # 阶段 3 完成——重试落回 PENDING，continue 落 SKIPPED）。
        if index < len(steps):
            step = steps[index]
            if step.status is WorkflowStepStatus.FAILED:
                step_definition = definition.steps[index]
                if step_definition.on_failure == "retry" and step.attempts < 2:
                    steps[index] = step.model_copy(
                        update={"status": WorkflowStepStatus.PENDING}
                    )
                    emit_local(
                        EventType.WORKFLOW_STEP_RETRY,
                        AgentRole.SUPERVISOR.value,
                        workflow_id=workflow.workflow_id,
                        workflow_step_id=step.step_id,
                        workflow_step_index=index + 1,
                    )
                elif step_definition.on_failure == "continue":
                    steps[index] = step.model_copy(
                        update={"status": WorkflowStepStatus.SKIPPED}
                    )
                else:
                    return self._workflow_failed_updates(
                        state,
                        workflow.model_copy(update={"steps": steps}),
                        definition,
                        ErrorCode.WORKFLOW_BUDGET_EXCEEDED,
                        sequence,
                    )

        # 阶段 3：推进越过全部终态步骤（revise 回退已把目标段重置，
        # 不会越过它）。
        while index < len(steps) and steps[index].status in {
            WorkflowStepStatus.COMPLETED,
            WorkflowStepStatus.SKIPPED,
        }:
            index += 1

        # 收口：全部步骤终态 → 回 Supervisor 整合作答
        if index >= len(steps):
            completed = workflow.model_copy(
                update={
                    "steps": steps,
                    "current_step_index": index,
                    "status": WorkflowStatus.COMPLETED,
                }
            )
            emit_local(
                EventType.WORKFLOW_COMPLETED,
                AgentRole.SUPERVISOR.value,
                workflow_id=workflow.workflow_id,
            )
            return cast(
                AgentState,
                {
                    "current_agent": AgentRole.SUPERVISOR.value,
                    "next_agent": AgentRole.SUPERVISOR.value,
                    "workflow": completed,
                    "iteration_budget": None,
                    "pending_tool_approval": None,
                    "run_error": None,
                    "handoff_count": state.get("handoff_count", 0),
                    "agent_switch_count": state.get("agent_switch_count", 0),
                    "events": emitted,
                },
            )

        # 分派步骤。RUNNING 到达此处（审批恢复后簿记前调度先到等边界
        # 场景）按重入语义处理：保留 RUNNING、attempts 递增、重新分派——
        # Worker 携共享历史重跑即可续上，不做响亮失败（真实冒烟教训：
        # 防御性 raise 会把可恢复状态变成整轮图异常终止）。
        step = steps[index]
        step_definition = definition.steps[index]
        instruction = definition.format_instruction(
            step_definition,
            workflow.params,
            artifact_dir=workflow.artifact_root,
        )
        if step.attempts > 0:
            # 重试提示注入（ppt-workflow-design §五-4）：重试分派的是同一
            # 模板，模型只能从历史自行归因；一行显式提示显著提高重试
            # 成功率（结构校验失败/落盘闸未过均适用）。
            instruction += (
                "\n[系统] 这是重试：上一次输出未通过结构校验或未产出"
                "有效产物，请严格遵循本步格式要求。"
            )
        steps[index] = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "attempts": step.attempts + 1,
            }
        )
        emit_local(
            EventType.WORKFLOW_STEP_STARTED,
            step.worker_role,
            workflow_id=workflow.workflow_id,
            workflow_step_id=step.step_id,
            workflow_step_index=index + 1,
        )
        emit_local(
            EventType.AGENT_SWITCHED,
            AgentRole.SUPERVISOR.value,
            content=step.worker_role,
        )
        return cast(
            AgentState,
            {
                "current_agent": AgentRole.SUPERVISOR.value,
                "next_agent": step.worker_role,
                "messages": [HumanMessage(content=instruction)],
                "iteration_budget": step_definition.iteration_budget,
                "workflow": workflow.model_copy(
                    update={"steps": steps, "current_step_index": index}
                ),
                "pending_tool_approval": None,
                "run_error": None,
                "handoff_count": state.get("handoff_count", 0),
                "agent_switch_count": state.get("agent_switch_count", 0),
                "events": emitted,
            },
        )

    def _workflow_failed_updates(
        self,
        state: AgentState,
        workflow: WorkflowState,
        definition: WorkflowDefinition,
        error_code: ErrorCode,
        sequence: int,
    ) -> AgentState:
        """工作流终局失败收口：FAILED 状态 + RUN_FAILED，路由判死。"""
        return cast(
            AgentState,
            {
                "current_agent": AgentRole.SUPERVISOR.value,
                "next_agent": None,
                "workflow": workflow.model_copy(
                    update={
                        "status": WorkflowStatus.FAILED,
                        "error_code": error_code,
                    }
                ),
                "pending_tool_approval": None,
                "run_error": RunError(
                    error_code=error_code,
                    message=f"工作流 {definition.title} 终止：{error_code.value}",
                    agent=AgentRole.SUPERVISOR.value,
                ),
                "events": [
                    RunEvent(
                        event_type=EventType.WORKFLOW_FAILED,
                        sequence=sequence + 1,
                        session_id=state.get("session_id"),
                        run_id=state.get("run_id"),
                        agent=AgentRole.SUPERVISOR.value,
                        success=False,
                        error_code=error_code,
                        workflow_id=workflow.workflow_id,
                    )
                ],
            },
        )

    def _approve_tool(self, state: AgentState) -> AgentState:
        """Pause before an exact side-effecting call, then execute only that call.

        The model-produced AIMessage is persisted by ``_wrap`` before routing
        here.  LangGraph may replay this gate while resolving ``interrupt()``,
        but the model node itself is never replayed.
        """
        raw_pending = state.get("pending_tool_approval")
        if raw_pending is None:
            raise RuntimeError("tool approval node requires a pending request")
        pending = ToolApprovalRequest.model_validate(raw_pending)
        raw_decision = interrupt(pending.model_dump(mode="json"))
        decision = ToolApprovalDecision.model_validate(raw_decision)
        executor = self.agents[pending.agent_role].tool_executor
        tool_call: dict[str, object] = {
            "id": pending.tool_call_id,
            "name": pending.tool_name,
            "args": pending.arguments,
            "type": "tool_call",
        }
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )
        emitted: list[RunEvent] = []
        try:
            stream_writer = get_stream_writer()
        except RuntimeError:
            stream_writer = lambda _value: None

        def emit(event_type: EventType, **values: Any) -> None:
            nonlocal sequence
            sequence += 1
            event = RunEvent(
                event_type=event_type,
                sequence=sequence,
                session_id=state.get("session_id"),
                run_id=state.get("run_id"),
                agent=pending.agent_role.value,
                tool_name=pending.tool_name,
                tool_call_id=pending.tool_call_id,
                **values,
            )
            emitted.append(event)
            stream_writer(
                {"kind": "run_event", "event": event.model_dump(mode="json")}
            )

        if decision.action is ToolApprovalAction.REJECT:
            execution = executor.reject(
                tool_call,
                pending.agent_role,
                ErrorCode.TOOL_APPROVAL_REJECTED,
            )
        else:
            def forward_output(channel: str, text: str) -> None:
                emit(
                    EventType.TOOL_OUTPUT,
                    content=text,
                    output_stream=cast(Literal["stdout", "stderr"], channel),
                )

            with (
                workspace_scope(
                    state.get("workspace_root"),
                    additional_roots=state.get("additional_workspace_roots", []),
                ),
                approved_shell_execution(),
                # officecli_edit 的运行时门与 shell 并列进入批准上下文
                # （计划 3.7：双保险缺一即无法写文件，高危 H3 的接线点）。
                approved_office_execution(),
                shell_output_scope(forward_output),
            ):
                execution = executor.execute(tool_call, pending.agent_role)

        emit(
            EventType.TOOL_COMPLETED,
            output_summary=tool_output_summary(execution.result.output),
            success=execution.result.success,
            duration_ms=execution.result.duration_ms,
            error_code=execution.result.error_code,
        )
        gate_updates: dict[str, object] = {
            "messages": [execution.message],
            "tool_results": [execution.result],
            "events": emitted,
            "next_agent": pending.agent_role.value,
            "pending_tool_approval": None,
            "run_error": None,
        }
        # T5-3：审批门是 requires_approval 工具的唯一执行路径，officecli_edit
        # 的生成文件在这里收集并写入 state 通道；_wrap 在后续终端回答上
        # 挂载并清空（见 AgentState.generated_files 注释）。
        gate_generated = _generated_files_from_tool_results([execution.result])
        if gate_generated:
            gate_updates["generated_files"] = gate_generated
        return cast(AgentState, gate_updates)

    def _approve_handoff(self, state: AgentState) -> AgentState:
        """暂停并提交分派决定；恢复时仅重放这个无外部副作用的 gate。"""
        pending = state.get("pending_handoff")
        if pending is None:
            raise RuntimeError("handoff approval node requires a pending proposal")
        proposal = HandoffApprovalRequest.model_validate(pending)
        raw_decision = interrupt(
            proposal.model_dump(mode="json", exclude_none=True)
        )
        decision = HandoffApprovalDecision.model_validate(raw_decision)
        sequence = max(
            (event.sequence for event in state.get("events", [])),
            default=-1,
        )

        if decision.action is HandoffApprovalAction.REJECT:
            # 拒绝不执行 Worker 或自动重规划；本轮以成功终止事件安全收口。
            reject_updates: dict[str, object] = {
                "next_agent": None,
                "pending_handoff": None,
                "run_error": None,
                "handoff_count": state.get("handoff_count", 0),
                "agent_switch_count": state.get("agent_switch_count", 0),
                "events": [
                    RunEvent(
                        event_type=EventType.RUN_COMPLETED,
                        sequence=sequence + 1,
                        session_id=state.get("session_id"),
                        run_id=state.get("run_id"),
                        agent=AgentRole.SUPERVISOR.value,
                        success=True,
                    )
                ],
            }
            rejected_plan = _task_plan_for_proposal(state, proposal)
            if rejected_plan is not None:
                reject_updates["task_plan"] = rejected_plan.model_copy(
                    update={"status": TaskPlanStatus.CANCELLED}
                )
            return cast(
                AgentState,
                reject_updates,
            )

        # 人工通过：决定字段优先，缺省回退到原提案
        target = decision.target_agent or proposal.target_agent
        task_content = decision.task_content or proposal.task_content
        planned = _task_plan_for_proposal(state, proposal)
        handoff_count = state.get("handoff_count", 0)
        switch_count = state.get("agent_switch_count", 0)
        limit_error: RunError | None = None
        if handoff_count + 1 > self.max_handoffs:
            limit_error = RunError(
                error_code=ErrorCode.GRAPH_HANDOFF_LIMIT,
                message=f"handoff 次数超过上限：{self.max_handoffs}",
                agent=AgentRole.SUPERVISOR.value,
            )
        elif switch_count + 1 > self.max_agent_switches:
            limit_error = RunError(
                error_code=ErrorCode.GRAPH_SWITCH_LIMIT,
                message=f"Agent 切换次数超过上限：{self.max_agent_switches}",
                agent=AgentRole.SUPERVISOR.value,
            )
        if limit_error is not None:
            failure_updates: dict[str, object] = {
                "next_agent": None,
                "pending_handoff": None,
                "run_error": limit_error,
                "handoff_count": handoff_count,
                "agent_switch_count": switch_count,
                "events": [
                    RunEvent(
                        event_type=EventType.RUN_FAILED,
                        sequence=sequence + 1,
                        session_id=state.get("session_id"),
                        run_id=state.get("run_id"),
                        agent=AgentRole.SUPERVISOR.value,
                        success=False,
                        error_code=limit_error.error_code,
                    )
                ],
            }
            if planned is not None:
                failure_updates["task_plan"] = planned.model_copy(
                    update={"status": TaskPlanStatus.FAILED}
                )
            return cast(
                AgentState,
                failure_updates,
            )

        updates: dict[str, object] = {
            "next_agent": target.value,
            "pending_handoff": None,
            "run_error": None,
            "handoff_count": handoff_count + 1,
            "agent_switch_count": switch_count + 1,
            "events": [
                RunEvent(
                    event_type=EventType.AGENT_SWITCHED,
                    sequence=sequence + 1,
                    session_id=state.get("session_id"),
                    run_id=state.get("run_id"),
                    agent=target.value,
                    success=True,
                )
            ],
        }
        # 计划型审批：把人工修正（目标/描述）写回当前计划步骤，保证后续调度一致
        if planned is not None:
            step_index = planned.current_step_index
            steps = list(planned.steps)
            steps[step_index] = steps[step_index].model_copy(
                update={
                    "target_agent": target,
                    "description": task_content,
                }
            )
            updates["task_plan"] = planned.model_copy(update={"steps": steps})
            updates["messages"] = [HumanMessage(content=task_content)]
        if decision.task_content is not None:
            task_context = state.get("task_context")
            updates["task_context"] = (
                TaskContext(description=decision.task_content)
                if task_context is None
                else TaskContext.model_validate(task_context).model_copy(
                    update={"description": decision.task_content}
                )
            )
            # 追加而非替换原始消息，既保留审计历史，也让 Worker 看到最新任务。
            updates["messages"] = [HumanMessage(content=decision.task_content)]
        return cast(AgentState, updates)

    @staticmethod
    def _route(
        state: AgentState,
        orchestration_mode: OrchestrationMode = "handoff",
    ) -> str:
        """有 handoff 时转给目标；Worker 完成后回到 Supervisor。

        orchestration_mode 由 build() 注册边时绑定（默认 handoff 兼容
        既有直调方）；tool 模式下活动计划不走分派节点，见下方分支注释。
        """
        if state.get("run_error") is not None:
            return "end"  # 已判死：终止
        if state.get("pending_tool_approval") is not None:
            return _TOOL_APPROVAL_NODE
        if state.get("pending_handoff") is not None:
            return _HANDOFF_APPROVAL_NODE  # 有审批请求：先去人工确认 gate
        next_agent = state.get("next_agent")
        if next_agent in {role.value for role in AgentRole}:
            return next_agent  # 模型指定了合法目标：直接转交
        # 固定工作流分支（lesson-workflow-design §二）：RUNNING/
        # PAUSED_APPROVAL 且无待分派动作时进调度节点。Worker 完成
        # （current=worker）与 Supervisor 触发后（current=supervisor）
        # 都汇聚于此；调度节点总是给出 next_agent（Worker 或收口
        # Supervisor），因此不会自环。收口 Supervisor 轮结束后工作流
        # 已 COMPLETED，分支不再命中，走下方既有收口逻辑。
        workflow = _workflow_from_state(state)
        if workflow is not None and workflow.status in {
            WorkflowStatus.RUNNING,
            WorkflowStatus.PAUSED_APPROVAL,
        }:
            return _WORKFLOW_DISPATCH_NODE
        plan = _task_plan_from_state(state)
        # 活动计划交调度节点分派——这是 handoff 模式的专属路径。tool 模式
        # 的计划在 Supervisor 的 ReAct 循环内经 ask_* 同步执行（S5-A1），
        # 若也走分派节点会绕开工具层门控与失败策略、把 Worker 当图节点
        # 直派（审查发现的管控旁路）；因此 tool 模式下 Supervisor 轮结束
        # 即运行收口，未完成的 ACTIVE 计划留在 checkpoint（下一用户轮
        # 由 create_initial_state 的轮次重置自然清空）。
        if (
            plan is not None
            and plan.status is TaskPlanStatus.ACTIVE
            and orchestration_mode == "handoff"
        ):
            return _TASK_PLAN_DISPATCH_NODE
        if state.get("current_agent") != AgentRole.SUPERVISOR.value:
            return AgentRole.SUPERVISOR.value  # Worker 收尾：交回 Supervisor
        return "end"  # 无待办：终止

    @staticmethod
    def _new_run_state(
        user_input: str,
        session_id: str,
        user_id: str | None,
        persisted_values: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        workspace_root: str | None = None,
        additional_workspace_roots: Sequence[str] = (),
        knowledge_namespace: str | None = None,
    ) -> AgentState:
        """为 invoke/stream 共用地构造本轮输入，避免两条入口语义漂移。"""
        # S5-C1 决策 4：会话绑定的知识空间经 extra 通道进入图，_wrap 按
        # learning_scope 先例注入 knowledge_scope（None = 未绑定）。
        namespace_extras = (
            {"knowledge_namespace": knowledge_namespace}
            if knowledge_namespace is not None
            else {}
        )
        active_run_id = run_id or str(uuid4())
        if persisted_values:
            return cast(
                AgentState,
                {
                    "messages": [HumanMessage(content=user_input)],
                    "run_id": active_run_id,
                    "workspace_root": workspace_root,
                    "additional_workspace_roots": list(additional_workspace_roots),
                    **({"extra": dict(namespace_extras)} if namespace_extras else {}),
                    "next_agent": None,
                    "pending_handoff": None,
                    "pending_tool_approval": None,
                    "intent": None,
                    "evaluation": None,
                    # P2-8：批改结论与 evaluation 同构、每轮重置
                    #（历史批改经消息元数据恢复，见 GRADING_METADATA_KEY）。
                    "grading": None,
                    "reference_verification": None,
                    "task_plan": None,
                    "task_results": [],
                    # 固定工作流不跨用户轮存活（排队语义见
                    # lesson-workflow-design §六）：新一轮不继承上一轮的
                    # 工作流进度；进行中的恢复只经审批暂停的同一 run。
                    "workflow": None,
                    # 步骤级 ReAct 预算随工作流一起每轮重置（调度节点
                    # 分派时再写入）
                    "iteration_budget": None,
                    # T5-3：生成文件回执按用户轮次重置（跨轮不累积，
                    # 新一轮回答不重复携带旧轮次的下载入口）
                    "generated_files": [],
                    "run_error": None,
                    "handoff_count": 0,
                    "agent_switch_count": 0,
                },
            )
        state = create_initial_state(
            session_id=session_id,
            user_id=user_id,
            run_id=active_run_id,
            workspace_root=workspace_root,
            additional_workspace_roots=additional_workspace_roots,
        )
        state["messages"] = [HumanMessage(content=user_input)]
        return state

    def stream(
        self,
        user_input: str,
        session_id: str = "demo",
        user_id: str | None = None,
        run_id: str | None = None,
        workspace_root: str | None = None,
        additional_workspace_roots: Sequence[str] = (),
        knowledge_namespace: str | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """直接转发 LangGraph messages/custom 流，供 API 实时消费。"""
        self._user_key(user_id)
        self._session_key(session_id)
        app = self.build()
        if self.checkpointer is None:
            state = self._new_run_state(
                user_input,
                session_id,
                user_id,
                run_id=run_id,
                workspace_root=workspace_root,
                additional_workspace_roots=additional_workspace_roots,
                knowledge_namespace=knowledge_namespace,
            )
            yield from cast(
                Iterator[tuple[str, Any]],
                app.stream(state, stream_mode=["messages", "custom"]),
            )
            return

        config = self._thread_config(session_id, user_id)
        with self._persistence_lock:
            snapshot = app.get_state(config)
            if snapshot.next:
                resume_method = (
                    "resume_tool_approval()"
                    if cast(AgentState, snapshot.values).get(
                        "pending_tool_approval"
                    )
                    is not None
                    else "resume_handoff()"
                    if snapshot.interrupts
                    else "resume()"
                )
                raise RuntimeError(
                    f"存在待恢复执行，请先调用 {resume_method}"
                )
            state = self._new_run_state(
                user_input,
                session_id,
                user_id,
                cast(Mapping[str, Any], snapshot.values),
                run_id=run_id,
                workspace_root=workspace_root,
                additional_workspace_roots=additional_workspace_roots,
                knowledge_namespace=knowledge_namespace,
            )
            yield from cast(
                Iterator[tuple[str, Any]],
                app.stream(
                    state,
                    config=config,
                    stream_mode=["messages", "custom"],
                ),
            )

    def run(
        self,
        user_input: str,
        session_id: str = "demo",
        user_id: str | None = None,
        run_id: str | None = None,
        workspace_root: str | None = None,
        additional_workspace_roots: Sequence[str] = (),
        knowledge_namespace: str | None = None,
    ) -> AgentState:
        """从一条用户消息启动协作图。"""
        self._user_key(user_id)
        self._session_key(session_id)
        app = self.build()
        if self.checkpointer is None:
            state = self._new_run_state(
                user_input,
                session_id,
                user_id,
                run_id=run_id,
                workspace_root=workspace_root,
                additional_workspace_roots=additional_workspace_roots,
                knowledge_namespace=knowledge_namespace,
            )
            return cast(AgentState, app.invoke(state))

        config = self._thread_config(session_id, user_id)
        with self._persistence_lock:
            snapshot = app.get_state(config)
            if snapshot.next:
                resume_method = (
                    "resume_tool_approval()"
                    if cast(AgentState, snapshot.values).get(
                        "pending_tool_approval"
                    )
                    is not None
                    else "resume_handoff()"
                    if snapshot.interrupts
                    else "resume()"
                )
                raise RuntimeError(
                    f"存在待恢复执行，请先调用 {resume_method}"
                )
            state = self._new_run_state(
                user_input,
                session_id,
                user_id,
                cast(Mapping[str, Any], snapshot.values),
                run_id=run_id,
                workspace_root=workspace_root,
                additional_workspace_roots=additional_workspace_roots,
                knowledge_namespace=knowledge_namespace,
            )
            return cast(AgentState, app.invoke(state, config=config))

    def resume(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> AgentState:
        """恢复普通 pending；动态人工断点必须由 resume_handoff 提交决定。"""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("resume requires a configured checkpointer")
        app = self.build()
        with self._persistence_lock:
            snapshot = app.get_state(config)
            if not snapshot.values or not snapshot.next:
                raise ValueError("当前会话没有待恢复执行")
            # next 同时表示普通 pending 与动态 interrupt；仅后者要求人工决定。
            if snapshot.interrupts:
                if cast(AgentState, snapshot.values).get(
                    "pending_tool_approval"
                ) is not None:
                    raise ValueError(
                        "当前会话等待人工工具决策，请调用 resume_tool_approval()"
                    )
                raise ValueError(
                    "当前会话等待人工 handoff 决策，请调用 resume_handoff()"
                )
            # None 输入表示「纯恢复」：不注入新消息，仅继续执行被打断的图
            return cast(AgentState, app.invoke(None, config=config))

    def resume_handoff(
        self,
        session_id: str,
        decision: HandoffApprovalDecision,
        user_id: str | None = None,
    ) -> AgentState:
        """校验 Interrupt ID，并恢复等待人工决定的 handoff gate。"""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("resume_handoff requires a configured checkpointer")
        app = self.build()
        with self._persistence_lock:
            snapshot = app.get_state(config)
            pending = _pending_handoff_from_snapshot(snapshot)
            if not snapshot.next or pending is None:
                raise ValueError("当前会话没有待人工确认的 handoff 断点")
            current_id = pending.interrupt_id
            if decision.interrupt_id != current_id:
                raise ValueError("interrupt_id 与当前 handoff 断点不匹配")
            # 用人工决定恢复被中断的 gate（resume 键以 interrupt_id 定位断点）
            command: Command[str] = Command(
                resume={
                    current_id: decision.model_dump(mode="json"),
                }
            )
            return cast(AgentState, app.invoke(command, config=config))

    def resume_tool_approval(
        self,
        session_id: str,
        decision: ToolApprovalDecision,
        user_id: str | None = None,
    ) -> AgentState:
        """Resume one exact approval-gated tool call and finish the graph."""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("resume_tool_approval requires a configured checkpointer")
        app = self.build()
        with self._persistence_lock:
            snapshot = app.get_state(config)
            pending = _pending_tool_approval_from_snapshot(snapshot)
            if not snapshot.next or pending is None:
                raise ValueError("当前会话没有待人工确认的工具断点")
            if decision.interrupt_id != pending.interrupt_id:
                raise ValueError("interrupt_id 与当前工具断点不匹配")
            command: Command[str] = Command(
                resume={
                    pending.interrupt_id: decision.model_dump(mode="json"),
                }
            )
            return cast(AgentState, app.invoke(command, config=config))

    def stream_tool_approval(
        self,
        session_id: str,
        decision: ToolApprovalDecision,
        user_id: str | None = None,
    ) -> Iterator[tuple[str, Any]]:
        """Resume a tool gate while exposing model, terminal and run events."""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("stream_tool_approval requires a configured checkpointer")
        app = self.build()
        with self._persistence_lock:
            snapshot = app.get_state(config)
            pending = _pending_tool_approval_from_snapshot(snapshot)
            if not snapshot.next or pending is None:
                raise ValueError("当前会话没有待人工确认的工具断点")
            if decision.interrupt_id != pending.interrupt_id:
                raise ValueError("interrupt_id 与当前工具断点不匹配")
            command: Command[str] = Command(
                resume={
                    pending.interrupt_id: decision.model_dump(mode="json"),
                }
            )
            yield from cast(
                Iterator[tuple[str, Any]],
                app.stream(
                    command,
                    config=config,
                    stream_mode=["messages", "custom"],
                ),
            )

    def get_pending_tool_approval(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> PendingToolApproval | None:
        """Return the exact pending tool call and its current interrupt ID."""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError(
                "get_pending_tool_approval requires a configured checkpointer"
            )
        app = self.build()
        with self._persistence_lock:
            return _pending_tool_approval_from_snapshot(app.get_state(config))

    def get_pending_handoff(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> PendingHandoffApproval | None:
        """从 checkpoint 返回可公开恢复的 handoff 断点；无待确认时返回 None。"""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError(
                "get_pending_handoff requires a configured checkpointer"
            )
        app = self.build()
        with self._persistence_lock:
            return _pending_handoff_from_snapshot(app.get_state(config))

    @staticmethod
    def _thread_config(session_id: str, user_id: str | None) -> RunnableConfig:
        user_key = CollaborativeAgentGraph._user_key(user_id)
        session_key = CollaborativeAgentGraph._session_key(session_id)
        # 长度前缀避免分隔符碰撞，none 明示匿名租户，实现 user+session 隔离。
        thread_id = f"user:{user_key}|session:{session_key}"
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _user_key(user_id: str | None) -> str:
        if user_id is None:
            return "none"
        if not user_id.strip():
            raise ValueError("user_id must not be empty")
        return f"value:{len(user_id)}:{user_id}"

    @staticmethod
    def _session_key(session_id: str) -> str:
        if not session_id.strip():
            raise ValueError("session_id must not be empty")
        return f"{len(session_id)}:{session_id}"

    def get_state(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> AgentState | None:
        """Return the latest persisted state for a user session."""
        config = self._thread_config(session_id, user_id)
        if self.checkpointer is None:
            raise ValueError("get_state requires a configured checkpointer")
        values = self.build().get_state(config).values
        if not values:
            return None
        return cast(AgentState, dict(values))

    def get_history(
        self,
        session_id: str,
        user_id: str | None = None,
    ) -> list[BaseMessage]:
        """Return the persisted messages for a user session.

        返回的消息与改动前完全同构：类型与 content 不变，只是助手消息
        （AIMessage）的 additional_kwargs 新增了 AGENT_ROLE_METADATA_KEY
        键。调用方可用 core.state.message_agent_role(message) 读出产出该
        消息的 Agent 角色（枚举值），用于前端角色徽章等展示。
        """
        state = self.get_state(session_id, user_id)
        if state is None:
            return []
        return list(state.get("messages", []))

    def get_node_info(self) -> dict[str, dict[str, str]]:
        """返回节点身份与 Prompt，便于调试和展示。

        注意：返回的是静态系统提示词（ROLE_PROMPTS 角色卡）。S2-T2 起，
        助学 Agent（learning_assistant）的讲解深度按学生水平分层，其运行时
        实际发给模型的 system prompt 是静态卡 + 「[当前学生水平:...]」动态
        水平段（见 prompts.learning_assistant_system_prompt，按
        state["level"] 每轮生成）；如需查看完整提示词，请对该角色调用该函数。
        """
        return {
            role.value: {
                "role": role.value,
                "prompt": agent.system_prompt,
            }
            for role, agent in self.agents.items()
        }


def _interleave_subagent_traces(
    state: AgentState,
    parent_events: Sequence[RunEvent],
    traces: Sequence[Sequence[RunEvent]],
) -> list[RunEvent]:
    """把子代理事件插回对应工具调用之间，并统一重排会话序号。"""
    combined: list[RunEvent] = []
    trace_index = 0
    subagent_tools = frozenset(_SUBAGENT_TOOL_NAMES.values())
    for event in parent_events:
        combined.append(event)
        if (
            event.event_type is EventType.TOOL_STARTED
            and event.tool_name in subagent_tools
            and trace_index < len(traces)
        ):
            combined.extend(
                child_event.model_copy(
                    update={"parent_tool_call_id": event.tool_call_id}
                )
                for child_event in traces[trace_index]
            )
            trace_index += 1

    previous_sequence = max(
        (event.sequence for event in state.get("events", [])),
        default=-1,
    )
    session_id = state.get("session_id")
    return [
        event.model_copy(
            update={
                "sequence": previous_sequence + index + 1,
                "session_id": session_id,
                "run_id": state.get("run_id"),
            }
        )
        for index, event in enumerate(combined)
    ]


def _citations_from_tool_results(
    tool_results: Sequence[ToolResult],
) -> list[Citation]:
    """从本轮工具结果中收集检索命中的结构化引用（S2-T4）。

    收集时机与来源（与 S2-T3 evidence_tool_names 同源思路）：
    tool_results 是 _wrap 收到的 updates["tool_results"]——即本次 Agent
    轮内新增的工具结果，不是跨轮历史累积，因此「本轮」的界定天然准确，
    不会把上一轮或历史轮的检索引用混入本轮回答。

    识别规则（按证据类型过滤检索类工具）：
    - 工具名在 _CITATION_TOOL_NAMES 中，且执行成功、输出非空；
    - 输出是 search_knowledge 的固定 JSON 结构（{"found":..., "hits":
      [{"content","score","citation"}]}），逐项取 "citation" 还原为
      Citation——SearchHit 本就含 Citation（knowledge/models.py），工具
      输出原样带出，这里只做读取还原，不重新构造、不伪造。

    去重与编号：按 chunk_id 去重（同一 chunk 可能被多次检索命中），
    保持工具结果出现顺序——编号稳定可预期（前端渲染的引用序号与列表
    下标一一对应）。去重只按 chunk_id：同一文档的不同 chunk 是不同引用。

    零命中不伪造的实现路径：未调用检索工具 / 检索无命中（found=False
    或 hits 为空）→ 返回空列表，调用方（_attach_references）不注入
    references 键，回答不带引用；单个结果解析失败（脏数据）逐项跳过
    （读取端宽容，与 _intent_from_results 同一哲学），不会击穿运行。
    """
    citations: list[Citation] = []
    seen_chunk_ids: set[str] = set()
    for result in tool_results:
        if (
            result.tool_name not in _CITATION_TOOL_NAMES
            or not result.success
            or not result.output
        ):
            continue
        try:
            payload = json.loads(result.output)
        except (TypeError, ValueError):
            # ValueError 已覆盖 json.JSONDecodeError（其父类），
            # 无需重复列举；解析失败视为脏数据，跳过该工具结果。
            continue
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            continue
        for hit in hits:
            raw = hit.get("citation") if isinstance(hit, dict) else None
            if not isinstance(raw, dict):
                continue
            try:
                citation = Citation.model_validate(raw)
            except (TypeError, ValidationError):
                continue
            if citation.chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(citation.chunk_id)
            citations.append(citation)
    return citations


def _generated_files_from_tool_results(
    tool_results: Sequence[ToolResult],
) -> list[GeneratedFile]:
    """从本轮 officecli_edit 成功结果中收集生成文件清单（T5-3）。

    写工具成功时输出 JSON 携带 generated_files 键（见 office_tools.py 的
    GENERATED_FILES_RESULT_KEY）；「本轮」同样由 updates["tool_results"]
    天然界定。解析失败/键缺失/项非法逐项跳过（宽容读取，与
    _citations_from_tool_results 同一哲学）；同一文件按 path 去重，
    保留最后一次修改的回执元数据（size/mtime_ns 取最新值）。
    """
    collected: dict[str, GeneratedFile] = {}
    for result in tool_results:
        if result.tool_name != "officecli_edit" or not result.success:
            continue
        try:
            payload = json.loads(result.output)
        except (TypeError, ValueError):
            continue
        raw_files = (
            payload.get(GENERATED_FILES_RESULT_KEY)
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(raw_files, list):
            continue
        for item in raw_files:
            try:
                entry = GeneratedFile.model_validate(item)
            except (TypeError, ValidationError):
                continue
            # dict 赋值去重：同一路径后出现的（更新的）覆盖先前的
            collected[entry.path] = entry
    return list(collected.values())


def _generated_files_from_state(value: object) -> list[GeneratedFile]:
    """宽容读取 state 通道中的生成文件回执。

    checkpoint 反序列化后模型实例与 dict 两种形态都可能出现（与既有
    tool_results 等通道的序列化语义一致），非法项逐项跳过。
    """
    if not isinstance(value, list):
        return []
    files: list[GeneratedFile] = []
    for item in value:
        if isinstance(item, GeneratedFile):
            files.append(item)
        elif isinstance(item, dict):
            try:
                files.append(GeneratedFile.model_validate(item))
            except ValidationError:
                continue
    return files


def _attach_generated_files(
    messages: Sequence[BaseMessage],
    tool_results: Sequence[ToolResult],
    pending_generated: Sequence[GeneratedFile] = (),
) -> tuple[list[BaseMessage], bool]:
    """把生成文件清单挂到本轮终端回答消息上（T5-3）。

    与 _attach_references 同一闸口语义：目标是本轮最后一个无
    tool_calls 的 AIMessage；无终端回答（如本轮只发起工具调用）时不挂，
    返回 attached=False 让调用方保留通道待后续轮次再挂。
    无生成文件时原样返回、不注入空键。
    """
    files = [
        *_generated_files_from_tool_results(tool_results),
        *pending_generated,
    ]
    if not files:
        return list(messages), False
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if not (isinstance(message, AIMessage) and not message.tool_calls):
            continue
        updated[index] = with_generated_files(message, files)
        return updated, True
    return updated, False


def _attach_references(
    messages: Sequence[BaseMessage],
    tool_results: Sequence[ToolResult],
) -> tuple[list[BaseMessage], ReferenceVerification | None]:
    """把本轮检索命中的引用挂到本轮终端回答消息上，并做真实性校验（S2-T4+S2-T5）。

    注入目标：本轮（本次 Agent 轮）最后一个无 tool_calls 的 AIMessage
    ——即「使用检索证据作答」的那条最终回答；中间带 tool_calls 的助手
    消息（工具调用请求）与 HumanMessage/ToolMessage 一律不挂。

    多轮协作口径（本任务的实现决定，写入注释）：每个 Agent 轮独立结算
    ——Worker 检索后作答，其回答消息携带引用；Supervisor 聚合回答由
    子任务输出拼接生成（_deterministic_aggregation），本身未执行检索，
    故不带引用，前端按消息渲染引用（与角色徽章同机制）。聚合改写
    （_replace_terminal_ai_output / _append_missing_results_notice）用
    model_copy 仅替换 content、原样保留 additional_kwargs，因此先注入
    的引用不会因聚合改写丢失（与角色元数据同一保障）。

    ── S2-T5 校验时机与依据（为什么在写入前校验）──
    校验层位于「收集之后、写入消息元数据之前」：本函数是引用进入
    state["messages"]（进而 checkpoint 持久化）的唯一闸口，写入前校验
    保证「落到消息上的每条引用都是本轮真实命中」这一不变式成立；若在
    写入后校验，伪造引用已经持久化，只能事后修补，且无法保证前端
    （D3-T5 按消息渲染引用）看到的列表纯净。
    校验依据（ground truth）：_citations_from_tool_results 从本轮
    tool_results 解析出的真实命中（chunk 级全集）——「本轮」由
    updates["tool_results"] 天然界定（只含本 Agent 轮新增的工具结果），
    不会把历史轮的检索混入。被校验对象：消息上**已存在**的引用
    （message_references 宽容读取）——正常路径下模型不产出引用，消息
    无引用；出现已有引用只可能是模型输出注入、历史脏数据或外部写入，
    这正是伪造引用的唯一来源，校验层逐一识别。
    处置取舍（剔除 vs 降级标记）：选「剔除」。理由：1) 引用列表的语义
    是「本轮真实证据的权威列表」，保留伪造条目会让前端渲染出点不开的
    坏链接（引用编号=列表下标，见下）；2) 剔除后列表紧凑、编号稳定，
    与 S2-T4「编号=出现顺序」的契约一致，无需前端处理「未验证」态；
    3) 剔除不是无痕的——校验结论（removed 计数与被剔除 chunk_id）写
    入 state["reference_verification"] 并并入评价结果，审计者可查；
    4) 不动既有 Citation 模型（不加 verified 字段），向后兼容风险最小。
    「降级标记」方案留给未来需要保留可疑引用的场景（届时
    ReferenceVerification.verified < total 即有语义）。

    边界：无检索命中 → 消息不带 references 键（「零命中不伪造」）；
    检索命中但本轮没有终端回答（如模型一直调用工具直到迭代超限）→
    没有可挂的消息，同样不挂——回答不存在时引用无意义。
    """
    # 第一步：收集本轮真实命中（chunk 级全集，校验与挂载的共同依据）。
    ground_truth = _citations_from_tool_results(tool_results)
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if not (isinstance(message, AIMessage) and not message.tool_calls):
            continue
        # 第二步：读取消息上已有的引用（读取端宽容，见 message_references）
        # 并与真实命中逐条比对，识别伪造/越界条目（判定逻辑见
        # _verify_references 注释；剔除后其余合法引用保留——它们本就
        # 是真实命中的子集，由下方最终列表覆盖）。
        existing = message_references(message) or []
        removed: list[Citation] = []
        if existing:
            _, removed = _verify_references(existing, ground_truth)
        # 第三步：文档级合并规范化（同一 document_id 的多个 chunk 合并
        # 为一条，规则与编号稳定性见 _merge_citations_by_document 注释）。
        final = _merge_citations_by_document(ground_truth)
        if final:
            # 挂载规范化后的引用；with_references 整体替换 references 键，
            # 因此消息上残留的伪造引用不会写入持久化历史。
            updated[index] = with_references(message, final)
        elif existing:
            # 全部引用被剔除且无真实命中可挂：剥离消息上已有的 references
            # 键（否则伪造引用仍会随消息写入 checkpoint），与「零命中不
            # 注入」语义一致。
            updated[index] = _strip_references(message)
        # 第四步：组装校验结论（无挂载且无剔除 → None，调用方不写 state）。
        verification = _reference_verification_from(ground_truth, final, removed)
        return updated, verification
    return updated, None


def _citation_fields_match(left: Citation, right: Citation) -> bool:
    """Citation 全字段比对（伪造判定用）。

    判定规则：除 chunk_id 外，document_id / source / page 也必须与真实
    命中一致——「引用指向的文档与页码」是引用真实性的组成部分，模型或
    脏数据若在真实 chunk_id 上篡改文档/页码（字段不匹配），同样按伪造
    处置（见任务验收「chunk_id 不在命中集、或字段不匹配」）。
    """
    return (left.document_id, left.source, left.page, left.chunk_id) == (
        right.document_id,
        right.source,
        right.page,
        right.chunk_id,
    )


def _verify_references(
    existing: Sequence[Citation],
    ground_truth: Sequence[Citation],
) -> tuple[list[Citation], list[Citation]]:
    """逐条校验消息中已有引用 vs 本轮真实命中，返回 (通过, 伪造)。

    伪造/越界判定（对应任务验收定义）：
    - chunk_id 不在本轮命中集（ground_truth 的 chunk_id 集合）→ 越界
      （声称引用了本轮根本没检索到的片段）；
    - chunk_id 在命中集但 Citation 其他字段不匹配 → 伪造（在真实
      chunk_id 上篡改 document_id/source/page，见 _citation_fields_match）。
    两者都进 removed 列表；通过校验的进 verified 列表（当前剔除策略下
    调用方只需 removed，verified 保留供未来「降级未验证」模式使用）。
    ground_truth 已由 _citations_from_tool_results 按 chunk_id 去重，
    因此 chunk_id → Citation 映射是单值的，不会出现同 id 多候选。
    """
    truth_by_chunk_id = {citation.chunk_id: citation for citation in ground_truth}
    verified: list[Citation] = []
    removed: list[Citation] = []
    for citation in existing:
        truth = truth_by_chunk_id.get(citation.chunk_id)
        if truth is not None and _citation_fields_match(citation, truth):
            verified.append(citation)
        else:
            removed.append(citation)
    return verified, removed


def _merge_citations_by_document(
    citations: Sequence[Citation],
) -> list[Citation]:
    """文档级合并规范化：同一 document_id 的多个 chunk 引用合并为一条。

    规则：保留「首次出现」的 chunk 的 Citation（含其 page/chunk_id），
    同文档后续 chunk 全部并入该条（不新增条目）。为什么保留第一条而非
    聚合 page 列表：Citation 模型没有 pages 字段，聚合需要改既有模型
    （向后兼容风险）；保留第一条则模型零改动，且输出稳定可解析——
    列表顺序 = 文档首次出现顺序，编号（列表下标）与文档一一对应，前端
    按编号渲染时同一文档永远只有一个编号，点击可定位到首个 chunk 的
    原文位置（chunk_id 坐标仍在）。

    为什么合并发生在写入端而不是收集端（_citations_from_tool_results
    保持 chunk 级全集）：真实命中全集是校验依据，若收集端就合并，同
    文档第二条 chunk 会被「地面真相」遗漏，消息里引用它时会被误判为
    伪造——合并只影响「最终挂载形态」，不影响「校验依据」。
    入参约定：citations 已按 chunk_id 去重（_citations_from_tool_results
    的保证），这里只做文档维度的第二次归并。
    """
    merged: list[Citation] = []
    seen_document_ids: set[str] = set()
    for citation in citations:
        if citation.document_id in seen_document_ids:
            continue
        seen_document_ids.add(citation.document_id)
        merged.append(citation)
    return merged


def _reference_verification_from(
    ground_truth: Sequence[Citation],
    final: Sequence[Citation],
    removed: Sequence[Citation],
) -> ReferenceVerification | None:
    """由校验/合并结果组装 ReferenceVerification；无内容时返回 None。

    无内容判定：既没有挂载任何引用（final 为空）也没有剔除任何伪造
    （removed 为空）→ 返回 None，调用方不写 state——与 evaluation 的
    「无评价 → None」同一语义，避免 checkpoint 出现「全零结论」噪音。
    字段口径（M-1 修正）：
    - verified 记录 **chunk 级**通过校验的条数（= ground_truth 条数）：
      最终挂载列表由 ground_truth 合并而来，chunk 级口径下每条都是真实
      命中；文档级合并后展示为 total 条（<= verified），因此
      merged = verified - total 自洽——审计者可同时看到「校验了多少个
      chunk、合并展示为几条」；
    - removed 仍是被剔除的伪造条数（来自消息已有引用，chunk 级）。
    合并明细（merged_document_ids）：ground_truth 中「同一文档第二次
    出现」的 chunk 所属 document_id——每个被合并文档只记录一次
    （M-2 修正：直接用「第二次出现才记录」生成，无需事后去重）。
    脱敏：明细只记 chunk_id / document_id 结构化标识，不复制引用正文。
    """
    if not final and not removed:
        return None
    merged_document_ids: list[str] = []
    seen_document_ids: set[str] = set()
    recorded_document_ids: set[str] = set()
    for citation in ground_truth:
        document_id = citation.document_id
        if document_id in seen_document_ids:
            # 同文档第二次出现：该文档被合并，记录一次后不再重复
            if document_id not in recorded_document_ids:
                recorded_document_ids.add(document_id)
                merged_document_ids.append(document_id)
        else:
            seen_document_ids.add(document_id)
    return ReferenceVerification(
        total=len(final),
        verified=len(ground_truth),
        removed=len(removed),
        merged=len(ground_truth) - len(final),
        removed_chunk_ids=[citation.chunk_id for citation in removed],
        merged_document_ids=merged_document_ids,
    )


def _strip_references(message: BaseMessage) -> BaseMessage:
    """返回移除 references 元数据键的消息副本（校验剔除后的清理动作）。

    仅当「消息上已有引用被全部剔除且无真实命中可挂」时调用：伪造引用
    不能随消息写入持久化历史，否则校验形同虚设。与 with_references
    同一副本语义（不修改原对象，避免污染模型返回对象被复用）；其他
    additional_kwargs（角色元数据等）原样保留。
    """
    additional_kwargs = dict(message.additional_kwargs)
    additional_kwargs.pop(REFERENCES_METADATA_KEY, None)
    return message.model_copy(update={"additional_kwargs": additional_kwargs})


def _handoff_target(tool_results: Sequence[ToolResult]) -> str | None:
    """只读取本次 Agent 调用产生的 handoff 结果。"""
    for result in reversed(tool_results):
        if result.tool_name == "handoff" and result.success:
            return result.output
    return None


def _intent_from_results(tool_results: Sequence[ToolResult]) -> Intent | None:
    """只读取本次 Supervisor 成功调用的最后一个 detect_intent 结果。

    写入端严格（工具 schema 用 Intent 枚举校验 + 工具函数输出固定 JSON），
    读取端宽容：解析失败返回 None 而非抛错——与 message_agent_role 的
    哲学一致，宁可让本轮退化为「无意图」也不让脏数据击穿运行。
    返回 None 表示模型未识别（或识别结果不可信），不会触发 UNCLEAR 拦截。
    """
    for result in reversed(tool_results):
        if result.tool_name == "detect_intent" and result.success:
            try:
                payload = json.loads(result.output)
                return Intent(str(payload["intent"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
    return None


def _level_from_results(tool_results: Sequence[ToolResult]) -> StudentLevel | None:
    """只读取本次 Supervisor 成功调用的最后一个 detect_level 结果。

    与 _intent_from_results 同一哲学（写入端严格、读取端宽容）：
    解析失败返回 None 而非抛错，本轮退化为「水平未知」，不击穿运行。
    与意图的关键差异：返回 None 只表示「本轮未更新水平画像」，
    不会清空 checkpoint 中已保留的旧水平（跨轮保留语义，见 state.py
    AgentState.level 注释）——是否覆盖旧值由调用方决定。
    """
    for result in reversed(tool_results):
        if result.tool_name == "detect_level" and result.success:
            try:
                payload = json.loads(result.output)
                return StudentLevel(str(payload["level"]))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
    return None


def _evaluation_from_results(
    tool_results: Sequence[ToolResult],
) -> dict[str, str] | None:
    """只读取本次 evaluator 成功调用的最后一个 submit_evaluation 结果。

    与 _intent_from_results 同一哲学（写入端严格、读取端宽容）：
    写入端由工具 schema 的枚举约束保证合法；读取端在此处再做一次
    枚举校验——历史脏数据或未来枚举变更产生的非法值抛 ValueError
    被捕获，返回 None 而非抛错，本轮退化为「无评价」，不击穿运行
    （也兼容未调 submit_evaluation 的旧行为与历史替身，见 _wrap 注释）。

    返回的 dict 只含四个模型填写的字段（verdict、fact_accuracy、
    citation_completeness、reason，枚举均为规范值字符串），证据工具名
    列表由调用方（_wrap）从本轮 tool_results 单独组装进
    EvaluationResult——证据是核心侧确定的「评价输入」记录，不由模型自报。
    """
    for result in reversed(tool_results):
        if result.tool_name == "submit_evaluation" and result.success:
            try:
                payload = json.loads(result.output)
                return {
                    "verdict": EvaluationVerdict(str(payload["verdict"])).value,
                    "fact_accuracy": EvaluationVerdict(
                        str(payload["fact_accuracy"])
                    ).value,
                    "citation_completeness": EvaluationVerdict(
                        str(payload["citation_completeness"])
                    ).value,
                    "reason": str(payload.get("reason", "")),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return None
    return None


def _grading_from_payload(payload: object) -> GradingResult | None:
    """把 submit_grading 的 JSON 负载构造为 GradingResult（核心侧组装）。

    与 _evaluation_from_results 同一哲学（写入端严格、读取端宽容）：
    逐题用 GradingItem 校验（score 超满分、非法字段等脏数据 → None，
    本轮退化为「无批改」，不击穿运行）；total_score / max_total_score
    由核心侧从 items 确定性汇总——不信任负载里模型自报的总分
    （与 evidence_tool_names「证据由核心侧确定」同一哲学）。
    """
    if not isinstance(payload, Mapping):
        return None
    try:
        raw_items = payload["items"]
        if not isinstance(raw_items, list) or not raw_items:
            return None
        items = [GradingItem.model_validate(item) for item in raw_items]
        return GradingResult(
            items=items,
            overall_comment=str(payload.get("overall_comment", "")),
            total_score=sum(item.score for item in items),
            max_total_score=sum(item.max_score for item in items),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _grading_from_results(
    tool_results: Sequence[ToolResult],
) -> tuple[GradingResult, str] | None:
    """handoff 模式：从本轮 evaluator 成功调用的最后一个 submit_grading
    结果解析批改结论（连同 tool_call_id，供落库幂等键使用）。

    读取端宽容与 _evaluation_from_results 一致：脏数据返回 None，
    不击穿运行。tool_call_id 缺失（不应发生）时以空串占位——复合
    幂等键 (tool_call_id, question_id) 仍互不冲突，只是失去重放保护。
    """
    for result in reversed(tool_results):
        if result.tool_name == "submit_grading" and result.success:
            try:
                payload = json.loads(result.output)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            grading = _grading_from_payload(payload)
            if grading is None:
                return None
            return grading, result.tool_call_id
    return None


def _grading_from_supervisor_results(
    tool_results: Sequence[ToolResult],
) -> tuple[GradingResult, str] | None:
    """tool 模式：从本轮成功的子代理工具（ask_*）输出提取批改结论。

    _run_subagent 在子代理成功调用 submit_grading 时把解析后的结论与
    tool_call_id 放进负载的 grading 键（见该方法注释）。角色守卫在
    调用方（_wrap 只在 SUPERVISOR 轮调用本函数）；读取端宽容：负载
    无 grading 键 / 结构非法 → None，不击穿运行。
    """
    subagent_tool_names = set(_SUBAGENT_TOOL_NAMES.values())
    for result in reversed(tool_results):
        if result.tool_name not in subagent_tool_names or not result.success:
            continue
        try:
            payload = json.loads(result.output)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        grading_block = payload.get("grading")
        if not isinstance(grading_block, Mapping):
            continue
        grading = _grading_from_payload(grading_block.get("result"))
        if grading is None:
            continue
        return grading, str(grading_block.get("tool_call_id", ""))
    return None


def _attach_grading(
    messages: Sequence[BaseMessage], grading: GradingResult
) -> list[BaseMessage]:
    """把批改结论挂到本轮终端回答消息（与 _attach_generated_files 同一
    闸口语义：最后一个无 tool_calls 的 AIMessage；无终端回答则不挂）。

    为什么挂消息元数据（P2-12 / pi 审查 🟡4）：grading 通道每轮重置、
    SessionProcess 只是末轮快照——挂消息后任意历史轮的批改卡刷新/
    切会话都能经 history 端点恢复。
    """
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if not (isinstance(message, AIMessage) and not message.tool_calls):
            continue
        updated[index] = with_grading(message, grading)
        break
    return updated


def _persist_grading_records(
    store: LearningRecordStore | None,
    grading: GradingResult,
    user_id: str | None,
    session_id: str | None,
    tool_call_id: str,
) -> None:
    """批改结果的确定性逐题落库（P2-10；pi 审查 🔴3：不靠模型自觉）。

    None 容忍守卫：store 未注入（既有测试构造点）或 user_id 缺失
    （未登录会话）时静默跳过——批改的 state 通道与消息元数据不受
    影响，只是学情诊断缺少该轮数据。复合幂等键
    (tool_call_id, question_id) 由 store 层 UNIQUE 约束保证重放不
    重复入库（pi 三轮审查 🟡3）。任何落库异常都被吞掉并记日志：
    落库是诊断的增强路径，不允许击穿批改主链路。
    """
    if store is None or user_id is None:
        return
    try:
        store.append_grading_records(
            [
                {
                    "question_id": item.question_id,
                    "score": item.score,
                    "max_score": item.max_score,
                    "knowledge_point": item.knowledge_point,
                    "error_tag": item.error_tag,
                }
                for item in grading.items
            ],
            user_id=user_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    except Exception:
        logging.getLogger("core.graph_builder").warning(
            "批改落库失败（不影响批改结论返回）", exc_info=True
        )


def _retrieval_decisions_from_results(
    tool_results: Sequence[ToolResult],
) -> list[dict[str, Any]]:
    """从本轮 search_knowledge 成功结果解析检索决策元数据（S4-T3）。

    与 _intent_from_results 同一哲学（写入端严格、读取端宽容）：
    - 只处理 tool_name == "search_knowledge" 且 success、输出非空的
      结果（复用 _CITATION_TOOL_NAMES 常量，新增检索类工具时一处
      生效）；
    - 输出是工具固定的 JSON 结构，metadata 键缺失（未启用自适应
      的旧路径输出）→ 跳过，不发事件——这是「默认零回归、无新
      事件」的落点（工具未注入 adaptive 配置时输出不含 metadata，
      历史 ToolResult 同样兼容）；
    - 解析失败 / 字段类型不合法 → 跳过该结果（脏数据不击穿运行，
      与 _intent_from_results 的宽容读取一致）；
    - 数值字段值域不合法（rounds / hit_count / top_score 为负数）→
      按 0 兜底，不跳过整条：负数只影响该字段，决策字段（needed /
      threshold_met / stopped_reason）仍可读；若原样透传，emit 时
      RunEvent 的 ge=0 校验会抛 ValidationError 击穿 _wrap（该 emit
      不在 try 内），故必须在解析层先兜底（I-1 修复，与「脏数据不
      击穿」承诺一致）。

    脱敏：只取决策摘要字段（needed / need_reason / threshold_met /
    stopped_reason / rounds / hit_count / top_score），不取每轮
    query——查询正文已在工具调用参数与 tool_results 审计中，事件
    载荷不重复记录（与 events.py 的 retrieval_* 字段注释同一口径）。
    """
    decisions: list[dict[str, Any]] = []
    for result in tool_results:
        if (
            result.tool_name not in _CITATION_TOOL_NAMES
            or not result.success
            or not result.output
        ):
            continue
        try:
            payload = json.loads(result.output)
        except (TypeError, ValueError):
            # ValueError 已覆盖 json.JSONDecodeError（其父类），
            # 解析失败视为脏数据，跳过该工具结果。
            continue
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(metadata, dict):
            # 无 metadata 键 = 未启用自适应检索（旧路径输出），
            # 不发检索决策事件。
            continue
        needed = metadata.get("needed")
        rounds = metadata.get("rounds")
        if not isinstance(needed, bool) or not (
            isinstance(rounds, int) and not isinstance(rounds, bool)
        ):
            # 核心字段缺失/类型不合法 → 脏数据，跳过（宽容读取）。
            continue
        threshold_met = metadata.get("threshold_met")
        hit_count = metadata.get("hit_count")
        top_score = metadata.get("top_score")
        need_reason = metadata.get("need_reason")
        stopped_reason = metadata.get("stopped_reason")
        decisions.append(
            {
                "needed": needed,
                "need_reason": need_reason if isinstance(need_reason, str) else "",
                "threshold_met": (
                    threshold_met if isinstance(threshold_met, bool) else None
                ),
                "stopped_reason": (
                    stopped_reason if isinstance(stopped_reason, str) else ""
                ),
                # 值域兜底（I-1）：类型合法但数值为负（脏数据）时按 0
                # 兜底——原样透传会让 RunEvent 的 ge=0 校验抛
                # ValidationError 击穿 _wrap（emit 不在 try 内）。
                # 选择「按 0 兜底」而非「跳过整条」：负数只影响该字段，
                # 决策字段仍可读，粒度最细（见函数 docstring）。
                # 用 max(x, 0) 表达「下限 0」而非三元式（ruff FURB136）。
                "rounds": max(rounds, 0),
                "hit_count": (
                    max(hit_count, 0)
                    if isinstance(hit_count, int)
                    and not isinstance(hit_count, bool)
                    else 0
                ),
                "top_score": (
                    max(top_score, 0.0)
                    if isinstance(top_score, (int, float))
                    and not isinstance(top_score, bool)
                    else 0.0
                ),
            }
        )
    return decisions


def _expected_artifact_path(
    workflow: WorkflowState,
    step_definition: WorkflowStepDefinition,
) -> Path | None:
    """requires_artifact 步骤的期望产物绝对路径（落盘闸判据）。

    无产物模板 / 无产物根 / 模板格式化失败（缺参数键等）→ None，
    判定按「未产出」处理（fail-closed）。
    """
    if (
        step_definition.artifact_filename_template is None
        or not workflow.artifact_root
    ):
        return None
    try:
        name = sanitize_artifact_filename(
            step_definition.artifact_filename_template.format(
                **workflow.params
            )
        )
    except (KeyError, ValueError):
        return None
    return Path(workflow.artifact_root) / name


def _workflow_from_results(tool_results: Sequence[ToolResult]) -> WorkflowState | None:
    """只解析本轮 Supervisor 经 start_workflow 启动的最后一个工作流。

    宽容读取与 _task_plan_from_results 同一哲学：冲突拒绝提示（非
    WorkflowState JSON）解析失败视为「本轮无启动」而不是崩溃。
    """
    for result in reversed(tool_results):
        if result.tool_name != "start_workflow" or not result.success:
            continue
        output = result.output or ""
        try:
            return WorkflowState.model_validate_json(output)
        except ValidationError:
            pass
        # 宽容回退：返回值尾部可能附着行为指令文本，取首个 JSON 对象
        decoder = json.JSONDecoder()
        try:
            _, offset = decoder.raw_decode(output.lstrip())
        except ValueError:
            continue
        try:
            parsed = json.loads(output.lstrip()[:offset])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            try:
                return WorkflowState.model_validate(parsed)
            except ValidationError:
                continue
    return None


def _workflow_from_state(state: AgentState) -> WorkflowState | None:
    """宽容读取持久化工作流：checkpoint 反序列化后可能是 dict。"""
    raw_workflow = state.get("workflow")
    return None if raw_workflow is None else WorkflowState.model_validate(raw_workflow)


def _bounded_workflow_summary(
    output: str | None,
    error_code: ErrorCode | None,
) -> str | None:
    """步骤终态的有界摘要：成功取终端输出前缀，失败记稳定错误码。"""
    if error_code is not None:
        return f"步骤失败：{error_code.value}"
    if output is None:
        return None
    limit = 400
    if len(output) <= limit:
        return output
    return f"{output[: limit - 3]}..."


def _task_plan_from_results(tool_results: Sequence[ToolResult]) -> TaskPlan | None:
    """只解析本次 Supervisor 成功创建的最后一个结构化计划。

    宽容读取：tool 模式的门控变体（create_task_plan_tool_mode）在冲突时
    返回成功执行的 JSON 拒绝提示（非计划正文）——解析失败视为「本轮无
    新计划」而不是崩溃（与仓库「读取端宽容」哲学一致；合法计划不受影响）。
    """
    for result in reversed(tool_results):
        if result.tool_name == "create_task_plan" and result.success:
            try:
                return TaskPlan.model_validate_json(result.output)
            except ValidationError:
                continue
    return None


# 宽容读取持久化计划：checkpoint 反序列化后可能是 dict，统一归一为模型
def _task_plan_from_state(state: AgentState) -> TaskPlan | None:
    raw_plan = state.get("task_plan")
    return None if raw_plan is None else TaskPlan.model_validate(raw_plan)


def _task_plan_for_proposal(
    state: AgentState,
    proposal: HandoffApprovalRequest,
) -> TaskPlan | None:
    """校验计划型审批仍对应 checkpoint 中未推进的当前步骤。"""
    if proposal.plan_step_sequence is None:
        return None
    plan = _task_plan_from_state(state)
    if plan is None or plan.status is not TaskPlanStatus.ACTIVE:
        raise RuntimeError("planned handoff requires an active task plan")
    step = plan.steps[plan.current_step_index]
    if step.sequence != proposal.plan_step_sequence:
        raise RuntimeError("planned handoff no longer matches current step")
    return plan


# 宽容读取任务结果列表（checkpoint 反序列化后可能是 dict）
def _task_results_from_state(state: AgentState) -> list[TaskStepResult]:
    return [
        TaskStepResult.model_validate(result)
        for result in state.get("task_results", [])
    ]


# 计划 Worker 轮开始前的防御性预检：目标角色或结果前缀不符则直接判死，不让 Worker 空跑
def _planned_worker_preflight(
    state: AgentState,
    role: AgentRole,
) -> tuple[TaskPlan | None, RunError | None]:
    plan = _task_plan_from_state(state)
    if role is AgentRole.SUPERVISOR or plan is None:
        return plan, None
    if plan.status is not TaskPlanStatus.ACTIVE:
        return plan, None
    step = plan.steps[plan.current_step_index]
    if step.target_agent is not role:
        return plan, RunError(
            error_code=ErrorCode.GRAPH_INVALID_TARGET,
            message=f"当前 Worker 与计划步骤目标不一致：{role.value}",
            agent=role.value,
        )
    try:
        _validate_task_result_prefix(plan, _task_results_from_state(state))
    except ValueError:
        return plan, RunError(
            error_code=ErrorCode.GRAPH_AGGREGATION_INVALID,
            message="已有任务结果与计划游标不一致",
            agent=role.value,
        )
    return plan, None


def _validate_task_result_prefix(
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> None:
    """结果必须是当前计划从第一步开始的连续、同角色前缀。"""
    if len(results) != plan.current_step_index:
        raise ValueError("task result count does not match plan cursor")
    for result, step in zip(
        results,
        plan.steps[: len(results)],
        strict=True,
    ):
        if (
            result.step_sequence != step.sequence
            or result.target_agent is not step.target_agent
        ):
            raise ValueError("task result does not match plan step")


# 计划已完成时取出全部子任务结果；不完整或游标不一致则抛错，由 _wrap 安全收口
def _ready_task_results(state: AgentState) -> list[TaskStepResult] | None:
    plan = _task_plan_from_state(state)
    if plan is None or plan.status is not TaskPlanStatus.COMPLETED:
        return None
    results = _task_results_from_state(state)
    _validate_task_result_prefix(plan, results)
    if len(results) != len(plan.steps):
        raise ValueError("completed plan is missing task results")
    return results


def _terminal_agent_output(messages: Sequence[BaseMessage]) -> str | None:
    """仅接收本次 ReAct 执行的终态文本 AIMessage。"""
    if not messages:
        return None
    message = messages[-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        return None
    output = message.text.strip()
    return output or None


# 把子任务结果拼成结构化 SystemMessage 注入 Supervisor 上下文，供聚合作答
def _task_results_message(
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> SystemMessage:
    payload = [
        {
            "step_sequence": result.step_sequence,
            "description": step.description,
            "target_agent": result.target_agent.value,
            "success": result.success,
            "output": result.output,
            "error_code": (
                result.error_code.value if result.error_code is not None else None
            ),
        }
        for step, result in zip(plan.steps, results, strict=True)
    ]
    return SystemMessage(
        content=(
            f"{_TASK_RESULTS_MARKER}\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        ),
        name="task_results",
    )


# 模型未产出聚合回答时的兜底：把成功/失败子任务结果拼成纯文本总结
def _deterministic_aggregation(
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> str:
    completed = [
        (
            f"#{result.step_sequence} "
            f"{plan.steps[result.step_sequence - 1].description}：{result.output}"
        )
        for result in results
        if result.success and result.output is not None
    ]
    sections = [
        "已完成部分：",
        "\n".join(completed) if completed else "无",
    ]
    failed = [result for result in results if not result.success]
    if failed:
        notices = [
            (
                f"#{result.step_sequence} "
                f"{plan.steps[result.step_sequence - 1].description}"
                f"（{result.error_code.value}）"
            )
            for result in failed
            if result.error_code is not None
        ]
        sections.extend(["", f"未完成子任务：{'；'.join(notices)}"])
    return "\n".join(sections)


# 用聚合文本覆盖终端回答（仅换 content，角色/引用元数据原样保留）
def _replace_terminal_ai_output(
    messages: Sequence[BaseMessage],
    content: str,
) -> list[BaseMessage]:
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if isinstance(message, AIMessage) and not message.tool_calls:
            # model_copy 仅替换 content，additional_kwargs 原样保留——
            # 因此 _wrap 注入的 agent 角色元数据与 S2-T4 的引用元数据
            # （references）在聚合改写内容后都不丢失。
            updated[index] = message.model_copy(update={"content": content})
            return updated
    raise RuntimeError("aggregation completed without a terminal AIMessage")


# 在聚合回答后追加「未完成子任务」提示，让用户看到失败项
def _append_missing_results_notice(
    messages: Sequence[BaseMessage],
    plan: TaskPlan,
    results: Sequence[TaskStepResult],
) -> list[BaseMessage]:
    failed = [result for result in results if not result.success]
    notices = [
        (
            f"#{result.step_sequence} "
            f"{plan.steps[result.step_sequence - 1].description}"
            f"（{result.error_code.value}）"
        )
        for result in failed
        if result.error_code is not None
    ]
    notice = f"未完成子任务：{'；'.join(notices)}"
    updated = list(messages)
    for index in range(len(updated) - 1, -1, -1):
        message = updated[index]
        if isinstance(message, AIMessage) and not message.tool_calls:
            answer = message.text.strip()
            content = (
                f"已完成部分：\n{answer}\n\n{notice}"
                if answer
                else f"已完成部分：无\n\n{notice}"
            )
            # 与 _replace_terminal_ai_output 同理：仅替换 content，
            # additional_kwargs（含 agent 角色元数据与 S2-T4 引用元数据）
            # 原样保留。
            updated[index] = message.model_copy(update={"content": content})
            return updated
    raise RuntimeError("aggregation completed without a terminal AIMessage")


# 终态输出无效时，把该角色的完成事件改标为失败（保证事件审计口径一致）
def _mark_agent_completion_invalid(
    events: Sequence[RunEvent],
    role: AgentRole,
) -> list[RunEvent]:
    updated = list(events)
    for index in range(len(updated) - 1, -1, -1):
        event = updated[index]
        if (
            event.event_type is EventType.AGENT_COMPLETED
            and event.agent == role.value
        ):
            updated[index] = event.model_copy(
                update={
                    "success": False,
                    "error_code": ErrorCode.AGENT_OUTPUT_INVALID,
                }
            )
            return updated
    raise RuntimeError("agent output validation requires a completion event")


def _latest_human_content(messages: Sequence[BaseMessage]) -> str:
    """读取本轮最近用户指令，作为人工确认时展示的初始任务。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _interrupt_identifier(pending_interrupt: object) -> str:
    """兼容 LangGraph 0.4 的 interrupt_id 与新版 id 字段。"""
    identifier = getattr(pending_interrupt, "id", None)
    if identifier is None:
        identifier = getattr(pending_interrupt, "interrupt_id", None)
    if not isinstance(identifier, str) or not identifier:
        raise RuntimeError("LangGraph interrupt 缺少稳定标识")
    return identifier


def _pending_tool_approval_from_snapshot(
    snapshot: StateSnapshot,
) -> PendingToolApproval | None:
    """Combine a checkpointed exact call with its dynamic interrupt ID."""
    values = cast(AgentState, snapshot.values)
    pending = values.get("pending_tool_approval")
    if pending is None:
        return None
    if len(snapshot.interrupts) != 1:
        raise RuntimeError("待确认工具调用必须对应且仅对应一个 interrupt")
    return PendingToolApproval(
        interrupt_id=_interrupt_identifier(snapshot.interrupts[0]),
        request=ToolApprovalRequest.model_validate(pending),
    )


def _pending_handoff_from_snapshot(
    snapshot: StateSnapshot,
) -> PendingHandoffApproval | None:
    """把 checkpoint 内部 interrupt 与 proposal 合成为稳定公开视图。"""
    values = cast(AgentState, snapshot.values)
    pending = values.get("pending_handoff")
    if pending is None:
        return None
    if len(snapshot.interrupts) != 1:
        raise RuntimeError("待确认 handoff 必须对应且仅对应一个 interrupt")
    return PendingHandoffApproval(
        interrupt_id=_interrupt_identifier(snapshot.interrupts[0]),
        request=HandoffApprovalRequest.model_validate(pending),
    )


__all__ = [
    "CollaborativeAgentGraph",
    "create_task_plan",
    "detect_intent",
    "detect_level",
    "handoff",
    "submit_evaluation",
]
